import time
import os
import json
from datetime import datetime, timezone

import httpx

from .. import config
from .types import QUESTION_TYPE_MAP, MODEL_MAP, deep_blank_model, WHITELISTED_QUESTION_TYPES
from ..config import GRAPHQL_URL, CONFIG_DIR
from .queries import (GET_STATE_QUERY, SAVE_RESPONSES_QUERY, SUBMIT_DRAFT_QUERY,
                      INITIATE_ATTEMPT_QUERY, ASSIGNMENT_FEEDBACK_QUERY)
from .cache import lookup_cached, store_answer, delete_cached
from loguru import logger
from ..llm.connector import (
    DEFAULT_RESPONSE_SCHEMA,
    GroqConnector,
    GeminiConnector,
    PerplexityConnector
)
from ..session_utils import get_csrf_headers, random_delay


# ============================================================
# SYSTEM PROMPT
# ============================================================
SYSTEM_PROMPT = (
    "Answer the provided questions. Be precise and concise.\n"
    "The questions are in a dict format where each key represents the question id, and the value is a JSON dict containing:\n"
    "- 'Question': the question text (which might have HTML tags, ignore them).\n"
    "- 'Options': a list of options (for MULTIPLE_CHOICE and CHECKBOX types) with option_id and value.\n"
    "- 'Type': one of 'MULTIPLE_CHOICE', 'CHECKBOX', 'TEXT_REFLECT', 'PLAIN_TEXT', 'TEXT_EXACT_MATCH', 'NUMERIC', 'MATH', 'REGEX', 'MULTIPLE_CHOICE_REFLECT', 'CHECKBOX_REFLECT', 'MULTIPLE_FILLABLE_BLANKS'.\n"
    "- 'previous_attempts': (optional, only for CHECKBOX) past attempt results.\n\n"
    "Rules for each question type:\n"
    "1. MULTIPLE_CHOICE / MULTIPLE_CHOICE_REFLECT: Single-choice question. Select exactly one option_id and place it in the 'chosen' list.\n"
    "2. CHECKBOX / CHECKBOX_REFLECT: Multi-choice question. Select one or more option_ids and place them in the 'chosen' list.\n"
    "3. TEXT_REFLECT / PLAIN_TEXT / TEXT_EXACT_MATCH: Fill-in-the-blank or short text answer. "
    "Put the text answer in the 'answer' field. If the question says 'in all lowercase', answer in lowercase.\n"
    "4. NUMERIC / MATH: Numeric or math answer. NO units, NO commas.\n"
    "5. REGEX: Regex pattern.\n"
    "6. MULTIPLE_FILLABLE_BLANKS: Multiple blanks. Return 'chosen' list of option_ids in order.\n\n"
    "SPECIAL RULES:\n"
    "1. DECIMAL TO BINARY: 13=1101, 10=1010, 15=1111\n"
    "2. NEGATIVE DECIMAL to two's complement (8-bit): -5=11111011, -1=11111111\n"
    "3. TWO'S COMPLEMENT: invert bits + add 1. One's complement: just invert bits\n"
    "4. BOOLEAN LAWS: x ∨ ¬x = TRUE, x ∧ ¬x = FALSE, DeMorgan: ¬(A∧B)=¬A∨¬B\n"
    "5. FILL-IN: 'html' lowercase, 'twos complement', 'embedded system', '1+1'='10'\n"
    "6. NUMERIC: 3.5 GHz = 3500000000 cycles\n"
    "7. CPU internal: ALU, Registers, Control Unit (CU). RAM/Cache NOT inside CPU.\n"
)

FINAL_ATTEMPT_PROMPT = (
    "\n\n=== FINAL ATTEMPT — HIGH ACCURACY MODE ===\n"
    "NO further attempts. You MUST achieve 100% correct.\n\n"
    "STEP 1: Read carefully. Identify concept.\n"
    "STEP 2: MC — eliminate wrong options one by one.\n"
    "STEP 3: CHECKBOX — include ONLY definitively correct. If unsure, skip.\n"
    "STEP 4: TEXT — lowercase if asked. Only answer, no explanation.\n"
    "STEP 5: Re-read and verify.\n\n"
    "TRAPS:\n"
    "- 'one limitation' → SPECIFIC limitation\n"
    "- DeMorgan's: ¬(A∧B) = ¬A∨¬B (NOT ¬A∧¬B)\n"
    "- Complement: x ∨ ¬x = TRUE, x ∧ ¬x = FALSE\n"
    "- Two's complement vs one's complement — read carefully!\n"
)

VERIFICATION_PROMPT = (
    "You are a strict answer verifier. Review the proposed answer.\n\n"
    "Return ONLY JSON:\n"
    "{\n"
    '  "verified": true/false,\n'
    '  "reason": "brief",\n'
    '  "corrected_chosen": ["option_id"],\n'
    '  "corrected_answer": "text"\n'
    "}\n\n"
    "If ANY doubt, mark verified=false and provide correction."
)


TYPE_LOOKUP = {
    "MULTIPLE_CHOICE": ("multipleChoiceResponse", "chosen"),
    "MULTIPLE_CHOICE_REFLECT": ("multipleChoiceReflectResponse", "chosen"),
    "CHECKBOX": ("checkboxResponse", "chosen"),
    "CHECKBOX_REFLECT": ("checkboxReflectResponse", "chosen"),
    "TEXT_REFLECT": ("textReflectResponse", "answer"),
    "PLAIN_TEXT": ("plainTextResponse", "plainText"),
    "TEXT_EXACT_MATCH": ("textExactMatchResponse", "answer"),
    "NUMERIC": ("numericResponse", "answer"),
    "MATH": ("mathResponse", "answer"),
    "REGEX": ("regexResponse", "answer"),
    "MULTIPLE_FILLABLE_BLANKS": ("multipleFillableBlanksResponse", "responses"),
}


MC_TYPES = {"MULTIPLE_CHOICE", "MULTIPLE_CHOICE_REFLECT"}
CB_TYPES = {"CHECKBOX", "CHECKBOX_REFLECT"}
TEXT_TYPES = {"TEXT_REFLECT", "PLAIN_TEXT", "TEXT_EXACT_MATCH", "NUMERIC", "MATH", "REGEX"}
FILLABLE_TYPES = {"MULTIPLE_FILLABLE_BLANKS"}

KNOWN_TYPES = MC_TYPES | CB_TYPES | TEXT_TYPES | FILLABLE_TYPES


def clean_id(val) -> str | None:
    if not val or not isinstance(val, str):
        return None
    if val.startswith("autoGradableResponseId~"):
        return val.split("~", 1)[1]
    if val.startswith("autoGradableResponse"):
        return None
    return val


def extract_part_id(question: dict, idx: int = 0) -> str | None:
    cleaned = clean_id(question.get("partId"))
    if cleaned:
        return cleaned

    schema = question.get("questionSchema") or {}
    if isinstance(schema, dict):
        cleaned = clean_id(schema.get("id") or schema.get("partId") or schema.get("submissionPartId"))
        if cleaned:
            return cleaned

    gs = question.get("gradeSettings") or {}
    if isinstance(gs, dict):
        cleaned = clean_id(gs.get("id") or gs.get("partId"))
        if cleaned:
            return cleaned

    cleaned = clean_id(question.get("submissionPartId"))
    if cleaned:
        return cleaned

    for resp_key in ("multipleChoiceResponse", "multipleChoiceReflectResponse",
                     "checkboxResponse", "checkboxReflectResponse",
                     "textReflectResponse", "plainTextResponse",
                     "textExactMatchResponse", "numericResponse",
                     "mathResponse", "regexResponse"):
        resp_obj = question.get(resp_key)
        if isinstance(resp_obj, dict):
            cleaned = clean_id(resp_obj.get("partId") or resp_obj.get("submissionPartId") or resp_obj.get("id"))
            if cleaned:
                return cleaned

    cleaned = clean_id(question.get("id"))
    if cleaned:
        return cleaned

    raw = question.get("partId")
    if raw and isinstance(raw, str):
        logger.warning(f"[Q{idx}] Using raw partId: {raw}")
        return raw

    logger.warning(f"[Q{idx}] No partId found. Keys: {list(question.keys())}")
    return None


class GradedSolver(object):
    def __init__(self, session: httpx.Client, course_id: str, item_id: str):
        self.session: httpx.Client = session
        self.course_id: str = course_id
        self.item_id: str = item_id
        self.attempt_id = None
        self.draft_id = None
        self.discarded_questions = []

        self.data_dir = CONFIG_DIR / "gradedData"
        os.makedirs(self.data_dir, exist_ok=True)

        self.data_file = os.path.join(
            self.data_dir, f"{course_id}~{item_id}.json")
        self.questions_data: dict = self._load_data()

    def _load_data(self) -> dict:
        if os.path.exists(self.data_file):
            with open(self.data_file, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    def _save_data(self) -> None:
        with open(self.data_file, "w", encoding="utf-8") as f:
            json.dump(self.questions_data, f, ensure_ascii=False, indent=4)

    def _get_response_text(self, options: list[dict], response):
        if isinstance(response, list):
            return [opt["value"] for opt in options if opt["option_id"] in response]
        for opt in options:
            if opt["option_id"] == response:
                return opt["value"]
        return response

    def _format_response(self, part_id: str, q_type: str,
                         chosen: list = None, answer: str = None,
                         fillable_responses: list = None) -> dict | None:
        lookup = TYPE_LOOKUP.get(q_type)
        if not lookup:
            logger.warning(f"Unknown question type: {q_type}")
            return None

        response_key, val_key = lookup

        if q_type in MC_TYPES:
            val = chosen[0] if chosen else None
            return {
                "questionId": part_id,
                "questionType": q_type,
                "questionResponse": {response_key: {val_key: val}}
            }
        elif q_type in CB_TYPES:
            val = chosen or []
            return {
                "questionId": part_id,
                "questionType": q_type,
                "questionResponse": {response_key: {val_key: val}}
            }
        elif q_type in FILLABLE_TYPES:
            return {
                "questionId": part_id,
                "questionType": q_type,
                "questionResponse": {
                    response_key: {"responses": fillable_responses or []}
                }
            }
        else:
            val = answer if answer is not None else None
            return {
                "questionId": part_id,
                "questionType": q_type,
                "questionResponse": {response_key: {val_key: val}}
            }

    def _get_connector(self):
        if config.GROQ_API_KEY:
            return GroqConnector()
        if config.GEMINI_API_KEY:
            return GeminiConnector()
        if config.PERPLEXITY_API_KEY:
            return PerplexityConnector()
        raise RuntimeError("No LLM API key specified.")

    def _count_known_options(self) -> tuple:
        correct = 0
        incorrect = 0
        for q in self.questions_data.values():
            for opt in q.get("Options", []):
                if opt.get("correct") is True:
                    correct += 1
                elif opt.get("correct") is False:
                    incorrect += 1
        return correct, incorrect

    def _verify_answers(self, connector, unsolved_questions, all_responses):
        corrected_responses = []
        for ans in all_responses:
            qid = ans.get("question_id")
            if qid not in unsolved_questions:
                corrected_responses.append(ans)
                continue

            q = unsolved_questions[qid]

            if q["Type"] in TEXT_TYPES or q["Type"] in FILLABLE_TYPES:
                corrected_responses.append(ans)
                continue

            verify_input = {
                "question": q["Question"],
                "options": q.get("Options", []),
                "type": q["Type"],
                "proposed_answer": {
                    "chosen": ans.get("chosen"),
                    "answer": ans.get("answer")
                }
            }

            try:
                verify_result = connector.get_response(
                    verify_input,
                    system_prompt=VERIFICATION_PROMPT,
                    response_schema=None
                )

                if isinstance(verify_result, str):
                    try:
                        verify_result = json.loads(verify_result.strip().strip("```json").strip("```"))
                    except Exception:
                        corrected_responses.append(ans)
                        continue

                if not verify_result.get("verified", True):
                    logger.warning(f"Verification failed for: {q['Question'][:60]}")
                    logger.warning(f"Reason: {verify_result.get('reason', 'N/A')}")

                    if verify_result.get("corrected_chosen"):
                        ans["chosen"] = verify_result["corrected_chosen"]
                        ans["answer"] = None
                    elif verify_result.get("corrected_answer"):
                        ans["answer"] = verify_result["corrected_answer"]
                        ans["chosen"] = None

            except Exception as e:
                logger.debug(f"Verification skipped ({e})")

            corrected_responses.append(ans)

        return corrected_responses

    # ============================================================
    # STANDARD SOLVE — 3 attempts, ALL questions in one batch
    # ============================================================
    def solve(self) -> bool:
        target_grade = 0.8
        attempt_count = 0
        MAX_ATTEMPTS = 3

        while True:
            if attempt_count >= MAX_ATTEMPTS:
                logger.warning(f"Reached max attempts ({MAX_ATTEMPTS}) — stopping")
                return False

            state = self.get_state()
            attempt_count += 1

            if state.get("outcome") and state["outcome"].get("isPassed") \
                    and state["outcome"].get("earnedGrade", 0) >= target_grade:
                logger.success("Already passed with target grade!")
                return True

            is_final_attempt = attempt_count >= MAX_ATTEMPTS
            if is_final_attempt:
                known_correct, known_incorrect = self._count_known_options()
                logger.warning("=" * 60)
                logger.warning(f"FINAL ATTEMPT — HIGH ACCURACY MODE (#{attempt_count})")
                logger.warning(f"Known-correct: {known_correct} | Known-incorrect: {known_incorrect}")
                logger.warning("=" * 60)

            allowed = state["allowedAction"]

            if allowed == "START_NEW_ATTEMPT":
                if not self.initiate_attempt():
                    logger.error("Could not start an attempt.")
                    return False
                continue
            elif allowed == "RESUME_DRAFT":
                logger.info("Resuming existing draft.")
            elif allowed is None:
                rate_limiter = state.get("attempts", {}).get("rateLimiterConfig") or {}
                increase_at = rate_limiter.get("attemptsRemainingIncreasesAt")
                retry_msg = ""
                if increase_at:
                    try:
                        dt_target = datetime.fromisoformat(increase_at.replace("Z", "+00:00"))
                        dt_now = datetime.now(timezone.utc)
                        delta = dt_target - dt_now
                        if delta.total_seconds() > 0:
                            hours = delta.days * 24 + delta.seconds // 3600
                            minutes = (delta.seconds % 3600) // 60
                            time_str = f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m"
                            retry_msg = f" (Retry in {time_str})"
                    except Exception:
                        retry_msg = f" (Retry from {increase_at})"
                if state.get("outcome") and state["outcome"].get("isPassed"):
                    logger.warning(f"Passed, but below target grade.{retry_msg}")
                    return True
                else:
                    logger.warning(f"No more attempts remaining!{retry_msg}")
                    return False
            else:
                logger.error(f"Unexpected allowedAction: {allowed}")
                return False

            self.discarded_questions = []
            questions = self.retrieve_questions(state)
            self._save_data()
            logger.info(f"Retrieved {len(questions)} questions from draft")

            unsolved_questions = {}
            answer_responses = []
            cache_hits = 0

            for part_id, q in questions.items():
                q_type = q["Type"]
                options = q.get("Options", [])

                already_solved = (
                    q.get("correct_answer")
                    or any(opt.get("correct") is True for opt in options)
                )
                if not already_solved:
                    cached = lookup_cached(q["Question"], options)
                    if cached:
                        if q_type in TEXT_TYPES and cached.get("answer"):
                            formatted = self._format_response(
                                part_id=part_id, q_type=q_type,
                                answer=cached["answer"])
                            if formatted:
                                answer_responses.append(formatted)
                                cache_hits += 1
                                continue
                        elif q_type in (MC_TYPES | CB_TYPES) and cached.get("chosen"):
                            matched_ids = [
                                opt["option_id"] for opt in options
                                if opt["value"] in cached["chosen"]
                            ]
                            if matched_ids:
                                formatted = self._format_response(
                                    part_id=part_id, q_type=q_type,
                                    chosen=matched_ids)
                                if formatted:
                                    answer_responses.append(formatted)
                                    cache_hits += 1
                                    continue

                if q_type in TEXT_TYPES:
                    if q.get("correct_answer"):
                        formatted = self._format_response(
                            part_id=part_id, q_type=q_type,
                            answer=q["correct_answer"])
                        if formatted:
                            answer_responses.append(formatted)
                    else:
                        unsolved_questions[part_id] = {
                            "Question": q["Question"], "Options": [],
                            "Type": q_type}

                elif q_type in FILLABLE_TYPES:
                    unsolved_questions[part_id] = {
                        "Question": q["Question"],
                        "Options": options,
                        "Type": q_type,
                        "BlankCount": q.get("BlankCount", 1)}

                elif q_type in MC_TYPES:
                    known_correct_id = next(
                        (opt["option_id"] for opt in options if opt.get("correct") is True), None)
                    if known_correct_id:
                        formatted = self._format_response(
                            part_id=part_id, q_type=q_type,
                            chosen=[known_correct_id])
                        if formatted:
                            answer_responses.append(formatted)
                        continue
                    filtered_options = [
                        opt for opt in options if opt.get("correct") is not False]
                    if len(filtered_options) == 1:
                        formatted = self._format_response(
                            part_id=part_id, q_type=q_type,
                            chosen=[filtered_options[0]["option_id"]])
                        if formatted:
                            answer_responses.append(formatted)
                        continue
                    unsolved_questions[part_id] = {
                        "Question": q["Question"],
                        "Options": filtered_options,
                        "Type": q_type}

                elif q_type in CB_TYPES:
                    all_resolved = all(
                        opt.get("correct") is not None for opt in options)
                    if all_resolved:
                        known_ids = [
                            opt["option_id"] for opt in options if opt.get("correct") is True]
                        formatted = self._format_response(
                            part_id=part_id, q_type=q_type,
                            chosen=known_ids)
                        if formatted:
                            answer_responses.append(formatted)
                        continue
                    filtered_options = [
                        opt for opt in options if opt.get("correct") is not False]
                    known_correct_vals = [
                        opt["value"] for opt in filtered_options if opt.get("correct") is True]
                    question_text = q["Question"]
                    if known_correct_vals:
                        question_text += "\n\n(IMPORTANT: These options are already CORRECT:\n"
                        for val in known_correct_vals:
                            question_text += f"- {val}\n"
                        question_text += ")"
                    unsolved_questions[part_id] = {
                        "Question": question_text,
                        "Options": filtered_options,
                        "Type": q_type}
                    incorrect_combs = q.get("incorrect_combinations", [])
                    if incorrect_combs:
                        virtual_feedbacks = []
                        for comb_entry in incorrect_combs:
                            comb = comb_entry["combination"]
                            score_info = f" (Score: {comb_entry.get('score')}/{comb_entry.get('max_score')})"
                            chosen_ids = [
                                opt["option_id"] for opt in filtered_options if opt["value"] in comb]
                            if chosen_ids:
                                virtual_feedbacks.append({
                                    "response": chosen_ids,
                                    "correctness": "INCORRECT",
                                    "hint": f"This combination was INCORRECT{score_info}."})
                        if virtual_feedbacks:
                            unsolved_questions[part_id]["previous_attempts"] = virtual_feedbacks

            if cache_hits:
                logger.success(f"Cache hits: {cache_hits}")

            logger.info(f"Unsolved: {len(unsolved_questions)}")

            # ============================================================
            # LLM CALL — ALL questions in ONE batch
            # ============================================================
            if unsolved_questions:
                connector = self._get_connector()

                if is_final_attempt:
                    active_prompt = SYSTEM_PROMPT + FINAL_ATTEMPT_PROMPT
                else:
                    active_prompt = SYSTEM_PROMPT

                logger.info(f"Sending ALL {len(unsolved_questions)} questions to LLM...")

                all_responses = []
                try:
                    llm_result = connector.get_response(
                        unsolved_questions, system_prompt=active_prompt,
                        response_schema=DEFAULT_RESPONSE_SCHEMA)
                    all_responses = llm_result.get("responses", [])
                    logger.info(f"LLM returned {len(all_responses)} responses")
                except Exception as e:
                    logger.error(f"LLM call failed: {e}")
                    all_responses = []

                if all_responses and is_final_attempt:
                    logger.info(f"Running self-verification on {len(all_responses)}...")
                    all_responses = self._verify_answers(
                        connector, unsolved_questions, all_responses)

                applied = 0
                for ans in all_responses:
                    qid = ans.get("question_id")
                    if not qid or qid not in unsolved_questions:
                        continue

                    q_type = unsolved_questions[qid]["Type"]

                    if q_type in FILLABLE_TYPES:
                        fillable_responses = []
                        chosen_list = ans.get("chosen") or []
                        for idx, opt_id in enumerate(chosen_list):
                            fillable_responses.append({
                                "multipleChoiceFillableBlankResponse": {
                                    "id": f"blank_{idx}",
                                    "optionId": opt_id
                                }
                            })
                        formatted = self._format_response(
                            part_id=qid, q_type=q_type,
                            fillable_responses=fillable_responses)
                    else:
                        formatted = self._format_response(
                            part_id=qid, q_type=q_type,
                            chosen=ans.get("chosen"),
                            answer=ans.get("answer"))

                    if formatted:
                        answer_responses.append(formatted)
                        applied += 1

                logger.info(f"Applied {applied} LLM responses")

                if applied == 0 and unsolved_questions:
                    logger.warning("LLM gave no responses — using fallback")
                    for qid, q in unsolved_questions.items():
                        q_type = q["Type"]
                        options = q.get("Options", [])

                        if q_type in MC_TYPES and options:
                            formatted = self._format_response(
                                part_id=qid, q_type=q_type,
                                chosen=[options[0]["option_id"]])
                        elif q_type in CB_TYPES and options:
                            formatted = self._format_response(
                                part_id=qid, q_type=q_type,
                                chosen=[options[0]["option_id"]])
                        elif q_type in TEXT_TYPES:
                            formatted = self._format_response(
                                part_id=qid, q_type=q_type, answer="1")
                        else:
                            continue
                        if formatted:
                            answer_responses.append(formatted)
                    logger.info(f"Fallback: {len(answer_responses)} responses")
            else:
                logger.info("All questions resolved locally.")

            logger.info(f"Total responses to save: {len(answer_responses)}")

            if not answer_responses:
                logger.error("No responses to save — skipping item")
                return True

            if not self.save_responses(answer_responses):
                logger.error("Could not save responses.")
                return False
            if not self.submit_draft():
                logger.error("Could not submit.")
                return False

            time.sleep(3.0)
            feedback_result = self.get_feedback()
            if feedback_result:
                outcome = feedback_result["outcome"]
                latest_score = outcome.get("latestScore", 0)
                max_score = outcome.get("maxScore", 1)
                earned_grade = latest_score / max_score if max_score else 0

                self._update_data_from_feedback(
                    feedback_result["parts"], answer_responses)
                self._save_data()

                logger.info(
                    f"Attempt {attempt_count} — Earned: {earned_grade:.1%}")

                if earned_grade >= target_grade:
                    logger.success(f"Passed on attempt {attempt_count}!")
                    return True

            random_delay(1.0, 2.0)

    # ============================================================
    # CURRENT QUIZ MODE
    # ============================================================
    def solve_current(self) -> bool:
        logger.info("=" * 60)
        logger.info("CURRENT QUIZ MODE")
        logger.info(f"Course: {self.course_id} | Item: {self.item_id}")
        logger.info("=" * 60)

        state = self.get_state()
        allowed = state.get("allowedAction")
        logger.info(f"Allowed action: {allowed}")

        if state.get("outcome"):
            outcome = state["outcome"]
            logger.info(f"Current outcome: passed={outcome.get('isPassed')}, "
                        f"grade={outcome.get('earnedGrade', 0):.2%}")

        attempts = state.get("attempts", {})
        logger.info(f"Attempts: made={attempts.get('attemptsMade')}, "
                    f"remaining={attempts.get('attemptsRemaining')}")

        previous_correctness = self._load_previous_correctness(state)
        if previous_correctness:
            correct_count = sum(1 for v in previous_correctness.values() if v == "CORRECT")
            incorrect_count = sum(1 for v in previous_correctness.values() if v == "INCORRECT")
            logger.info(f"Loaded: {correct_count} correct, {incorrect_count} incorrect")

        if state.get("outcome") and state["outcome"].get("isPassed"):
            logger.success("Already passed!")
            return True

        if allowed == "START_NEW_ATTEMPT":
            if not self.initiate_attempt():
                logger.error("Could not start attempt.")
                return False
            state = self.get_state()
        elif allowed == "RESUME_DRAFT":
            logger.info("Resuming existing draft.")
        elif allowed is None:
            logger.warning("No attempts remaining!")
            return False

        self.discarded_questions = []
        questions = self.retrieve_questions(state)
        self._save_data()

        unsolved_questions = {}
        answer_responses = []
        skipped_correct = 0
        cache_hits = 0

        for part_id, q in questions.items():
            q_type = q["Type"]
            options = q.get("Options", [])

            if previous_correctness.get(part_id) == "CORRECT":
                reused = False

                if q_type in TEXT_TYPES and q.get("correct_answer"):
                    formatted = self._format_response(
                        part_id=part_id, q_type=q_type,
                        answer=q["correct_answer"])
                    if formatted:
                        answer_responses.append(formatted)
                        reused = True
                elif q_type in MC_TYPES:
                    known_id = next(
                        (opt["option_id"] for opt in options if opt.get("correct") is True), None)
                    if known_id:
                        formatted = self._format_response(
                            part_id=part_id, q_type=q_type,
                            chosen=[known_id])
                        if formatted:
                            answer_responses.append(formatted)
                            reused = True
                elif q_type in CB_TYPES:
                    known_ids = [opt["option_id"] for opt in options if opt.get("correct") is True]
                    if known_ids:
                        formatted = self._format_response(
                            part_id=part_id, q_type=q_type,
                            chosen=known_ids)
                        if formatted:
                            answer_responses.append(formatted)
                            reused = True

                if reused:
                    skipped_correct += 1
                    continue

            already_solved = (
                q.get("correct_answer")
                or any(opt.get("correct") is True for opt in options)
            )
            if not already_solved:
                cached = lookup_cached(q["Question"], options)
                if cached:
                    if q_type in TEXT_TYPES and cached.get("answer"):
                        formatted = self._format_response(
                            part_id=part_id, q_type=q_type,
                            answer=cached["answer"])
                        if formatted:
                            answer_responses.append(formatted)
                            cache_hits += 1
                            continue
                    elif q_type in (MC_TYPES | CB_TYPES) and cached.get("chosen"):
                        matched_ids = [
                            opt["option_id"] for opt in options
                            if opt["value"] in cached["chosen"]
                        ]
                        if matched_ids:
                            formatted = self._format_response(
                                part_id=part_id, q_type=q_type,
                                chosen=matched_ids)
                            if formatted:
                                answer_responses.append(formatted)
                                cache_hits += 1
                                continue

            if q_type in TEXT_TYPES:
                if q.get("correct_answer"):
                    formatted = self._format_response(
                        part_id=part_id, q_type=q_type,
                        answer=q["correct_answer"])
                    if formatted:
                        answer_responses.append(formatted)
                else:
                    unsolved_questions[part_id] = {
                        "Question": q["Question"], "Options": [],
                        "Type": q_type}

            elif q_type in FILLABLE_TYPES:
                unsolved_questions[part_id] = {
                    "Question": q["Question"],
                    "Options": options,
                    "Type": q_type,
                    "BlankCount": q.get("BlankCount", 1)}

            elif q_type in MC_TYPES:
                known_id = next(
                    (opt["option_id"] for opt in options if opt.get("correct") is True), None)
                if known_id:
                    formatted = self._format_response(
                        part_id=part_id, q_type=q_type,
                        chosen=[known_id])
                    if formatted:
                        answer_responses.append(formatted)
                    continue
                filtered_options = [
                    opt for opt in options if opt.get("correct") is not False]
                if len(filtered_options) == 1:
                    formatted = self._format_response(
                        part_id=part_id, q_type=q_type,
                        chosen=[filtered_options[0]["option_id"]])
                    if formatted:
                        answer_responses.append(formatted)
                    continue
                unsolved_questions[part_id] = {
                    "Question": q["Question"],
                    "Options": filtered_options,
                    "Type": q_type}

            elif q_type in CB_TYPES:
                all_resolved = all(opt.get("correct") is not None for opt in options)
                if all_resolved:
                    known_ids = [opt["option_id"] for opt in options if opt.get("correct") is True]
                    formatted = self._format_response(
                        part_id=part_id, q_type=q_type, chosen=known_ids)
                    if formatted:
                        answer_responses.append(formatted)
                    continue

                filtered_options = [
                    opt for opt in options if opt.get("correct") is not False]
                known_correct_vals = [
                    opt["value"] for opt in filtered_options if opt.get("correct") is True]
                question_text = q["Question"]
                if known_correct_vals:
                    question_text += "\n\n(Known CORRECT options — MUST include:\n"
                    for val in known_correct_vals:
                        question_text += f"- {val}\n"
                    question_text += ")"

                unsolved_questions[part_id] = {
                    "Question": question_text,
                    "Options": filtered_options,
                    "Type": q_type}

                incorrect_combs = q.get("incorrect_combinations", [])
                if incorrect_combs:
                    virtual_feedbacks = []
                    for comb_entry in incorrect_combs:
                        comb = comb_entry["combination"]
                        score_info = f" (Score: {comb_entry.get('score')}/{comb_entry.get('max_score')})"
                        chosen_ids = [
                            opt["option_id"] for opt in filtered_options if opt["value"] in comb]
                        if chosen_ids:
                            virtual_feedbacks.append({
                                "response": chosen_ids,
                                "correctness": "INCORRECT",
                                "hint": f"This combination was INCORRECT{score_info}."})
                    if virtual_feedbacks:
                        unsolved_questions[part_id]["previous_attempts"] = virtual_feedbacks

        logger.info(f"Skipped (correct): {skipped_correct}")
        logger.info(f"Cache hits: {cache_hits}")
        logger.info(f"To solve: {len(unsolved_questions)}")

        if unsolved_questions:
            connector = self._get_connector()
            active_prompt = SYSTEM_PROMPT + FINAL_ATTEMPT_PROMPT

            logger.info(f"Sending ALL {len(unsolved_questions)} questions to LLM...")

            all_responses = []
            try:
                llm_result = connector.get_response(
                    unsolved_questions, system_prompt=active_prompt,
                    response_schema=DEFAULT_RESPONSE_SCHEMA)
                all_responses = llm_result.get("responses", [])
                logger.info(f"LLM returned {len(all_responses)} responses")
            except Exception as e:
                logger.error(f"LLM call failed: {e}")
                all_responses = []

            if all_responses:
                logger.info(f"Running self-verification...")
                all_responses = self._verify_answers(
                    connector, unsolved_questions, all_responses)

            applied = 0
            for ans in all_responses:
                qid = ans.get("question_id")
                if not qid or qid not in unsolved_questions:
                    continue

                q_type = unsolved_questions[qid]["Type"]

                if q_type in FILLABLE_TYPES:
                    fillable_responses = []
                    chosen_list = ans.get("chosen") or []
                    for idx, opt_id in enumerate(chosen_list):
                        fillable_responses.append({
                            "multipleChoiceFillableBlankResponse": {
                                "id": f"blank_{idx}",
                                "optionId": opt_id
                            }
                        })
                    formatted = self._format_response(
                        part_id=qid, q_type=q_type,
                        fillable_responses=fillable_responses)
                else:
                    formatted = self._format_response(
                        part_id=qid, q_type=q_type,
                        chosen=ans.get("chosen"),
                        answer=ans.get("answer"))

                if formatted:
                    answer_responses.append(formatted)
                    applied += 1

            logger.info(f"Applied {applied} LLM responses")

            if applied == 0 and unsolved_questions:
                logger.warning("LLM gave no responses — using fallback")
                for qid, q in unsolved_questions.items():
                    q_type = q["Type"]
                    options = q.get("Options", [])
                    if q_type in MC_TYPES and options:
                        formatted = self._format_response(
                            part_id=qid, q_type=q_type,
                            chosen=[options[0]["option_id"]])
                    elif q_type in CB_TYPES and options:
                        formatted = self._format_response(
                            part_id=qid, q_type=q_type,
                            chosen=[options[0]["option_id"]])
                    elif q_type in TEXT_TYPES:
                        formatted = self._format_response(
                            part_id=qid, q_type=q_type, answer="1")
                    else:
                        continue
                    if formatted:
                        answer_responses.append(formatted)

        logger.info(f"Total responses: {len(answer_responses)}")

        if not answer_responses:
            logger.error("No responses — skipping")
            return True

        if not self.save_responses(answer_responses):
            logger.error("Could not save responses.")
            return False
        if not self.submit_draft():
            logger.error("Could not submit.")
            return False

        logger.success("Quiz submitted!")
        time.sleep(3.0)

        feedback_result = self.get_feedback()
        if feedback_result:
            outcome = feedback_result["outcome"]
            latest_score = outcome.get("latestScore", 0)
            max_score = outcome.get("maxScore", 1)
            earned_grade = latest_score / max_score if max_score else 0
            self._update_data_from_feedback(feedback_result["parts"], answer_responses)
            self._save_data()
            logger.info(f"Score: {earned_grade:.1%}")
            if earned_grade >= 0.8:
                logger.success("Passed!")
                return True

        return True

    def _load_previous_correctness(self, state: dict) -> dict:
        result = {}

        for part_id, q in self.questions_data.items():
            options = q.get("Options", [])
            if q.get("correct_answer"):
                result[part_id] = "CORRECT"
                continue
            if options:
                all_resolved = all(opt.get("correct") is not None for opt in options)
                if all_resolved and any(opt.get("correct") is True for opt in options):
                    result[part_id] = "CORRECT"

        try:
            fb_result = self.get_feedback(max_retries=1)
            if not fb_result or not fb_result.get("parts"):
                return result

            for part in fb_result["parts"]:
                part_id = part.get("partId", "")
                if not part_id:
                    continue

                fb = part.get("feedback", {})
                correctness = fb.get("correctness")

                if correctness == "CORRECT":
                    result[part_id] = "CORRECT"
                    if part_id in self.questions_data:
                        self.questions_data[part_id]["previous_correctness"] = "CORRECT"
                    continue
                if correctness == "INCORRECT":
                    result[part_id] = "INCORRECT"
                    continue

                schema_options = (part.get("questionSchema") or {}).get("options") or []
                if not schema_options:
                    continue

                response_key = None
                for k in ("multipleChoiceResponse", "multipleChoiceReflectResponse",
                          "checkboxResponse", "checkboxReflectResponse"):
                    if k in part and part[k]:
                        response_key = k
                        break

                if not response_key:
                    continue

                resp = part[response_key] or {}
                chosen = resp.get("chosen")
                if isinstance(chosen, str):
                    chosen = [chosen]
                elif not isinstance(chosen, list):
                    chosen = []

                if not chosen:
                    continue

                correct_chosen = 0
                wrong_chosen = 0
                for opt in schema_options:
                    opt_id = opt.get("optionId")
                    if opt_id not in chosen:
                        continue
                    if "correctlyAnswered" in opt:
                        if opt["correctlyAnswered"]:
                            correct_chosen += 1
                        else:
                            wrong_chosen += 1

                if wrong_chosen == 0 and correct_chosen == len(chosen):
                    result[part_id] = "CORRECT"
                elif wrong_chosen > 0:
                    result[part_id] = "INCORRECT"

            correct_count = sum(1 for v in result.values() if v == "CORRECT")
            incorrect_count = sum(1 for v in result.values() if v == "INCORRECT")
            logger.info(f"Correctness: {correct_count} correct, {incorrect_count} incorrect")

        except Exception as e:
            logger.debug(f"Feedback load skipped: {e}")

        return result

    # ============================================================
    # SHARED API
    # ============================================================
    def get_state(self) -> dict:
        res = self.session.post(url=GRAPHQL_URL, headers=get_csrf_headers(self.session), params={
            "opname": "QueryState"}, json={
            "operationName": "QueryState",
            "variables": {"courseId": self.course_id, "itemId": self.item_id},
            "query": GET_STATE_QUERY}).json()
        return res["data"]["SubmissionState"]["queryState"]

    def initiate_attempt(self) -> bool:
        res = self.session.post(url=GRAPHQL_URL, headers=get_csrf_headers(self.session), params={
            "opname": "Submission_StartAttempt"}, json={
            "operationName": "Submission_StartAttempt",
            "variables": {"courseId": self.course_id, "itemId": self.item_id},
            "query": INITIATE_ATTEMPT_QUERY})
        return "Submission_StartAttemptSuccess" in res.text

    def retrieve_questions(self, state: dict) -> dict:
        draft = state["attempts"]["inProgressAttempt"]
        self.attempt_id = draft["id"]
        self.draft_id = draft["draft"]["id"]
        questions = draft["draft"]["parts"]
        questions_formatted = {}

        for idx, question in enumerate(questions):
            if not question.get("__typename") in QUESTION_TYPE_MAP:
                continue

            part_id = extract_part_id(question, idx)

            if not part_id:
                continue

            if not question.get("__typename") in WHITELISTED_QUESTION_TYPES:
                self.discarded_questions.append({
                    "questionId": part_id,
                    "questionType": QUESTION_TYPE_MAP[question["__typename"]][1],
                    "questionResponse": {
                        QUESTION_TYPE_MAP[question["__typename"]][0]:
                        deep_blank_model(MODEL_MAP[question["__typename"]])}})
                continue

            existing = self.questions_data.get(part_id, {})
            existing_options = existing.get("Options", [])
            existing_correctness = {}
            for opt in existing_options:
                if opt.get("correct") is not None:
                    existing_correctness[opt["value"]] = opt["correct"]

            options = []
            options_schema = question.get("questionSchema", {}).get("options") or []
            for option in options_schema:
                val = option["display"]["cmlValue"]
                options.append({
                    "option_id": option["optionId"],
                    "value": val,
                    "correct": existing_correctness.get(val, None)})

            blank_count = 1
            schema = question.get("questionSchema", {})
            fillable_blanks = schema.get("fillableBlanks") or []
            if fillable_blanks:
                blank_count = len(fillable_blanks)

            questions_formatted[part_id] = {
                "Question": question["questionSchema"]["prompt"]["cmlValue"],
                "Options": options,
                "Type": QUESTION_TYPE_MAP[question["__typename"]][1],
                "BlankCount": blank_count}

            if "correct_answer" in existing:
                questions_formatted[part_id]["correct_answer"] = existing["correct_answer"]
            if "incorrect_combinations" in existing:
                questions_formatted[part_id]["incorrect_combinations"] = existing["incorrect_combinations"]

        logger.info(f"Retrieved {len(questions_formatted)} valid questions (total {len(questions)})")

        self.questions_data.update(questions_formatted)
        return questions_formatted

    def save_responses(self, answer_responses: list) -> bool:
        valid_responses = []
        for resp in answer_responses:
            q_type = resp.get("questionType", "")
            if q_type not in KNOWN_TYPES:
                logger.warning(f"Skipping unknown type: {q_type}")
                continue

            q_resp = resp.get("questionResponse", {})
            has_content = False
            for key, val in q_resp.items():
                if isinstance(val, dict):
                    for inner in val.values():
                        if inner not in (None, "", [], {}):
                            has_content = True
                            break
                elif val not in (None, "", [], {}):
                    has_content = True
                if has_content:
                    break

            if has_content:
                valid_responses.append(resp)
            else:
                logger.warning(f"Skipping empty: {q_type}")

        valid_discarded = []
        for dq in self.discarded_questions:
            if dq.get("questionType") not in KNOWN_TYPES:
                continue
            resp = dq.get("questionResponse", {})
            has_value = False
            for key in resp:
                inner = resp[key] or {}
                if isinstance(inner, dict):
                    for v in inner.values():
                        if v not in (None, "", [], {}):
                            has_value = True
                            break
                if has_value:
                    break
            if has_value:
                valid_discarded.append(dq)

        payload = [*valid_responses, *valid_discarded]

        logger.info(f"Saving {len(payload)} responses "
                    f"({len(valid_responses)} valid, {len(valid_discarded)} discarded)")

        if not payload:
            logger.warning("Nothing to save")
            return False

        # TRY 1: Cleaned IDs
        res = self.session.post(
            url=GRAPHQL_URL,
            headers=get_csrf_headers(self.session),
            params={"opname": "Submission_SaveResponses"},
            json={
                "operationName": "Submission_SaveResponses",
                "variables": {
                    "input": {
                        "courseId": self.course_id,
                        "itemId": self.item_id,
                        "attemptId": self.attempt_id,
                        "questionResponses": payload}},
                "query": SAVE_RESPONSES_QUERY})

        if "Submission_SaveResponsesSuccess" in res.text:
            try:
                data = res.json()
                self.draft_id = (data["data"]["Submission_SaveResponses"]
                                 ["submissionState"]["attempts"]
                                 ["inProgressAttempt"]["draft"]["id"])
            except (KeyError, TypeError):
                pass
            return True

        # TRY 2: Restore original partIds
        logger.warning("Save failed with cleaned IDs — retrying with original partIds...")

        try:
            state = self.get_state()
            draft = state["attempts"]["inProgressAttempt"]
            original_questions = draft["draft"]["parts"]

            id_map = {}
            for idx, q in enumerate(original_questions):
                raw_part = q.get("partId") or q.get("id")
                cleaned = clean_id(raw_part)
                if cleaned and raw_part and cleaned != raw_part:
                    id_map[cleaned] = raw_part

            retry_payload = []
            for r in payload:
                qid = r.get("questionId")
                if qid in id_map:
                    new_r = dict(r)
                    new_r["questionId"] = id_map[qid]
                    retry_payload.append(new_r)
                else:
                    retry_payload.append(r)

            res2 = self.session.post(
                url=GRAPHQL_URL,
                headers=get_csrf_headers(self.session),
                params={"opname": "Submission_SaveResponses"},
                json={
                    "operationName": "Submission_SaveResponses",
                    "variables": {
                        "input": {
                            "courseId": self.course_id,
                            "itemId": self.item_id,
                            "attemptId": self.attempt_id,
                            "questionResponses": retry_payload}},
                    "query": SAVE_RESPONSES_QUERY})

            if "Submission_SaveResponsesSuccess" in res2.text:
                try:
                    data = res2.json()
                    self.draft_id = (data["data"]["Submission_SaveResponses"]
                                     ["submissionState"]["attempts"]
                                     ["inProgressAttempt"]["draft"]["id"])
                except (KeyError, TypeError):
                    pass
                logger.success("Save succeeded with original partIds!")
                return True
        except Exception as e:
            logger.error(f"Retry failed: {e}")

        logger.error(f"Save failed. Details:")
        for r in payload[:5]:
            logger.error(f"  Type: {r.get('questionType')}")
            logger.error(f"  questionId: {r.get('questionId')}")

        try:
            err_data = res.json()
            logger.error(json.dumps(err_data, indent=2)[:1500])
        except Exception:
            logger.error(res.text[:1500])

        return False

    def submit_draft(self) -> bool:
        res = self.session.post(url=GRAPHQL_URL, headers=get_csrf_headers(self.session), params={
            "opname": "Submission_SubmitLatestDraft"}, json={
            "operationName": "Submission_SubmitLatestDraft",
            "query": SUBMIT_DRAFT_QUERY,
            "variables": {"input": {
                "courseId": self.course_id,
                "itemId": self.item_id,
                "submissionId": self.draft_id}}})
        return "Submission_SubmitLatestDraftSuccess" in res.text

    def get_feedback(self, max_retries: int = 3) -> dict | None:
        for i in range(max_retries):
            res = self.session.post(url=GRAPHQL_URL, headers=get_csrf_headers(self.session), params={
                "opname": "AssignmentFeedback"}, json={
                "operationName": "AssignmentFeedback",
                "variables": {"courseId": self.course_id, "itemId": self.item_id},
                "query": ASSIGNMENT_FEEDBACK_QUERY}).json()
            try:
                feedback = res["data"]["SubmissionState"]["queryState"]["feedback"]
            except (KeyError, TypeError):
                return None
            if feedback is not None:
                parts = feedback.get("parts")
                if parts is not None and all(part.get("feedback") is not None for part in parts):
                    return feedback
            logger.warning(f"Feedback not ready ({i + 1}/{max_retries})")
            random_delay()
        return None

    def _update_data_from_feedback(self, feedback_parts: list, submitted_responses: list) -> None:
        question_lookup = {}
        for part_id in self.questions_data:
            key = part_id.split("~")[-1]
            question_lookup[key] = part_id

        response_lookup = {}
        for resp in submitted_responses:
            lookup = TYPE_LOOKUP.get(resp["questionType"])
            if not lookup:
                continue
            response_key, val_key = lookup
            response_lookup[resp["questionId"]] = resp["questionResponse"][response_key][val_key]

        for part in feedback_parts:
            feedback_part_id = part.get("partId", "")
            feedback_key = feedback_part_id.split("~")[-1]
            our_part_id = question_lookup.get(feedback_key)
            if not our_part_id:
                continue

            fb = part.get("feedback", {})
            correctness = fb.get("correctness")
            outcome = fb.get("autoGradedFeedbackOutcome") or {}
            submitted_chosen = response_lookup.get(our_part_id)
            our_q = self.questions_data[our_part_id]

            all_options = our_q.get("Options", [])
            question_type = our_q["Type"]

            if question_type in TEXT_TYPES:
                if correctness == "CORRECT" and not our_q.get("correct_answer"):
                    our_q["correct_answer"] = submitted_chosen
                    store_answer(
                        our_q["Question"], all_options,
                        answer=submitted_chosen)
                elif correctness == "INCORRECT":
                    # DELETE from cache — wrong answer
                    delete_cached(our_q["Question"], all_options)
                continue

            if question_type in FILLABLE_TYPES:
                continue

            is_single = question_type in MC_TYPES
            chosen_texts = set()
            if submitted_chosen:
                texts = self._get_response_text(all_options, submitted_chosen)
                chosen_texts = {texts} if isinstance(texts, str) else set(texts)

            schema_options = (part.get("questionSchema") or {}).get("options") or []
            for opt in schema_options:
                val = opt["display"].get("cmlValue")
                if not val or "correctlyAnswered" not in opt:
                    continue
                was_chosen = val in chosen_texts
                is_correct = opt["correctlyAnswered"]

                for our_opt in all_options:
                    if our_opt["value"] == val:
                        if is_correct and was_chosen:
                            our_opt["correct"] = True
                        elif not is_correct and was_chosen:
                            our_opt["correct"] = False

            if correctness == "CORRECT":
                for our_opt in all_options:
                    if our_opt["value"] in chosen_texts:
                        our_opt["correct"] = True
                if is_single:
                    for our_opt in all_options:
                        if our_opt["value"] not in chosen_texts:
                            our_opt["correct"] = False

                # Store correct answer to cache
                if question_type in MC_TYPES and chosen_texts:
                    store_answer(our_q["Question"], all_options,
                                 chosen=[next(iter(chosen_texts))])
                elif question_type in CB_TYPES and chosen_texts:
                    store_answer(our_q["Question"], all_options,
                                 chosen=list(chosen_texts))

            elif correctness == "INCORRECT":
                # DELETE from cache — wrong answer
                delete_cached(our_q["Question"], all_options)

                if is_single and chosen_texts:
                    chosen_text = next(iter(chosen_texts))
                    for our_opt in all_options:
                        if our_opt["value"] == chosen_text:
                            our_opt["correct"] = False
                            break
                elif not is_single and chosen_texts:
                    our_q.setdefault("incorrect_combinations", [])
                    comb = sorted(list(chosen_texts))
                    if not any(c["combination"] == comb for c in our_q["incorrect_combinations"]):
                        our_q["incorrect_combinations"].append({
                            "combination": comb,
                            "score": outcome.get("score"),
                            "max_score": outcome.get("maxScore")})