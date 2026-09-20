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
from .cache import lookup_cached, store_answer
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
    "- 'Type': one of 'MULTIPLE_CHOICE', 'CHECKBOX', 'TEXT_REFLECT', 'PLAIN_TEXT', 'TEXT_EXACT_MATCH', 'NUMERIC', 'MATH', 'REGEX'.\n"
    "- 'previous_attempts': (optional, only for CHECKBOX) past attempt results.\n\n"
    "Rules for each question type:\n"
    "1. MULTIPLE_CHOICE: Single-choice question. Select exactly one option_id and place it in the 'chosen' list.\n"
    "2. CHECKBOX: Multi-choice question. Select one or more option_ids and place them in the 'chosen' list.\n"
    "3. TEXT_REFLECT / PLAIN_TEXT / TEXT_EXACT_MATCH: Fill-in-the-blank or short text answer. "
    "Put the text answer in the 'answer' field. If the question says 'in all lowercase', answer in lowercase.\n"
    "4. NUMERIC / MATH: Numeric or math answer. Put the exact number or formula in the 'answer' field. "
    "NO units, NO commas, NO formatting — just the number.\n"
    "5. REGEX: Regex pattern. Put the pattern in the 'answer' field.\n\n"
    "IMPORTANT for CHECKBOX:\n"
    "If a question has 'previous_attempts', each entry records a prior submission of chosen option_ids:\n"
    "- 'response' is a list of option_ids that were chosen together.\n"
    "- 'hint' states that this combination was graded INCORRECT and shows the fractional score earned.\n"
    "Use these partial scores to logically deduce the status of options.\n\n"
    "SPECIAL RULES FOR SPECIFIC QUESTION PATTERNS:\n\n"
    "1. DECIMAL TO BINARY CONVERSION:\n"
    "   - 13 = 1101, 10 = 1010, 15 = 1111, 7 = 111, 5 = 101, 9 = 1001\n"
    "   - Negative decimal to two's complement (8-bit): -5 = 11111011, -1 = 11111111, -2 = 11111110\n"
    "   - Two's complement method: invert bits + add 1\n"
    "   - One's complement: just invert bits\n"
    "   - Two's complement of 1101 = 0011 (invert → 0010, add 1 → 0011)\n"
    "   - Provide answer WITHOUT '0b' prefix\n\n"
    "2. BOOLEAN ALGEBRA LAWS:\n"
    "   - Complement Law: x ∨ ¬x = TRUE (1), x ∧ ¬x = FALSE (0)\n"
    "   - DeMorgan's: ¬(A ∧ B) = ¬A ∨ ¬B, ¬(A ∨ B) = ¬A ∧ ¬B\n"
    "   - Identity Law: x ∨ 0 = x, x ∧ 1 = x\n"
    "   - Idempotent Law: x ∨ x = x, x ∧ x = x\n"
    "   - Absorption: x ∨ (x ∧ y) = x, x ∧ (x ∨ y) = x\n\n"
    "3. FILL-IN-THE-BLANK:\n"
    "   - If asked 'answer in all lowercase', MUST be lowercase\n"
    "   - 'Primary language for web development' → 'html' (lowercase)\n"
    "   - 'Binary representation system for positive and negative integers' → 'twos complement'\n"
    "   - 'Computer designed for specialized tasks' → 'embedded system'\n"
    "   - '1+1 in binary' → '10'\n"
    "   - 'Two's complement of 1101' → '0011'\n\n"
    "4. K-MAP GROUPING:\n"
    "   - Valid: adjacent cells, cells at edges wrap, powers of 2 (1,2,4,8,16)\n"
    "   - Invalid: diagonal grouping, non-power-of-2 groupings\n\n"
    "5. LIMITATION/ADVANTAGE QUESTIONS:\n"
    "   - Read carefully — 'limitation' means DISADVANTAGE\n"
    "   - 'One limitation of Boolean algebra' → 'becomes complex with many variables'\n\n"
    "6. COMPUTER SYSTEM COMPONENTS:\n"
    "   - CPU: executes instructions\n"
    "   - CPU internal: ALU, Registers, Control Unit (CU)\n"
    "   - RAM: volatile memory (NOT inside CPU)\n"
    "   - Cache: frequently accessed data (NOT inside CPU itself)\n"
    "   - ROM: non-volatile, holds system instructions\n\n"
    "7. NUMERIC QUESTIONS:\n"
    "   - Provide EXACT numeric value (no units, no commas)\n"
    "   - 3.5 GHz = 3500000000 cycles per second\n"
    "   - Typical modern RAM: 8 or 16 (in GB)\n"
    "   - Two's complement -5 in 8 bits = 11111011\n\n"
)

FINAL_ATTEMPT_PROMPT = (
    "\n\n=== CRITICAL: HIGH ACCURACY MODE ===\n"
    "You MUST achieve 100% correct answers. There will be NO further attempts.\n\n"
    "MANDATORY REASONING PROCESS — Follow these steps for EVERY question:\n\n"
    "STEP 1: Read the question carefully. Identify what concept is being tested.\n"
    "STEP 2: For MULTIPLE_CHOICE: Eliminate each wrong option one by one with a specific reason.\n"
    "STEP 3: For CHECKBOX: Evaluate each option independently. Include ONLY options that are "
    "definitively correct. If unsure, DO NOT include it.\n"
    "STEP 4: For TEXT_REFLECT/PLAIN_TEXT/TEXT_EXACT_MATCH: Answer in lowercase if asked. "
    "Provide ONLY the answer, no explanation.\n"
    "STEP 5: For NUMERIC/MATH: Provide the exact number. No units, no commas.\n"
    "STEP 6: Before finalizing, re-read the question and your chosen answer.\n\n"
    "COMMON TRAPS TO AVOID:\n"
    "- Questions asking for 'one limitation' — pick the SPECIFIC limitation\n"
    "- Numerical conversions — double-check the math\n"
    "- DeMorgan's theorem — ¬(A∧B) = ¬A∨¬B (NOT ¬A∧¬B)\n"
    "- Complement law — x ∨ ¬x = TRUE, x ∧ ¬x = FALSE\n"
    "- Fill-in-the-blank — answer precisely with expected format\n"
    "- Two's complement vs one's complement — read carefully!\n\n"
)

VERIFICATION_PROMPT = (
    "You are a strict answer verifier for a Coursera quiz. "
    "Review the proposed answer and confirm or correct it.\n\n"
    "Return ONLY a JSON object with this schema:\n"
    "{\n"
    '  "verified": true/false,\n'
    '  "reason": "brief explanation",\n'
    '  "corrected_chosen": ["option_id_1", ...] (only if verified is false and type is MC/CHECKBOX),\n'
    '  "corrected_answer": "text" (only if verified is false and type is TEXT/NUMERIC)\n'
    "}\n\n"
    "Be strict. If the proposed answer has ANY doubt, mark verified=false and provide the correction."
)


TYPE_LOOKUP = {
    "MULTIPLE_CHOICE": ("multipleChoiceResponse", "chosen"),
    "CHECKBOX": ("checkboxResponse", "chosen"),
    "TEXT_REFLECT": ("textReflectResponse", "answer"),
    "PLAIN_TEXT": ("plainTextResponse", "plainText"),
    "TEXT_EXACT_MATCH": ("textExactMatchResponse", "answer"),
    "NUMERIC": ("numericResponse", "answer"),
    "MATH": ("mathResponse", "answer"),
    "REGEX": ("regexResponse", "answer"),
}


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
                         chosen: list = None, answer: str = None) -> dict:
        response_key, val_key = TYPE_LOOKUP[q_type]
        if q_type == "MULTIPLE_CHOICE":
            val = chosen[0] if chosen else None
        elif q_type == "CHECKBOX":
            val = chosen or []
        else:
            val = answer if answer is not None else None
        return {
            "questionId": part_id,
            "questionType": q_type,
            "questionResponse": {
                response_key: {val_key: val}
            }
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
        """Second-pass verification: AI checks its own answers."""
        corrected_responses = []
        for ans in all_responses:
            qid = ans.get("question_id")
            if qid not in unsolved_questions:
                corrected_responses.append(ans)
                continue

            q = unsolved_questions[qid]

            # Skip verification for text/numeric — AI usually gets these right
            if q["Type"] in ("TEXT_REFLECT", "PLAIN_TEXT", "TEXT_EXACT_MATCH",
                             "NUMERIC", "MATH", "REGEX"):
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
                    verify_result = json.loads(verify_result.strip().strip("```json").strip("```"))

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
    # STANDARD SOLVE
    # ============================================================
    def solve(self) -> bool:
        target_grade = 0.8
        attempt_count = 0

        while True:
            state = self.get_state()
            attempt_count += 1

            if state.get("outcome") and state["outcome"].get("isPassed") \
                    and state["outcome"].get("earnedGrade", 0) >= target_grade:
                logger.success("Already passed with target grade!")
                return True

            is_final_attempt = attempt_count >= 3
            if is_final_attempt:
                known_correct, known_incorrect = self._count_known_options()
                logger.warning("=" * 60)
                logger.warning(f"3RD ATTEMPT — FULL EFFORT MODE (#{attempt_count})")
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
                        if q_type in ("TEXT_REFLECT", "PLAIN_TEXT", "TEXT_EXACT_MATCH",
                                      "NUMERIC", "MATH", "REGEX") and cached.get("answer"):
                            answer_responses.append(self._format_response(
                                part_id=part_id, q_type=q_type,
                                answer=cached["answer"]))
                            cache_hits += 1
                            continue
                        elif q_type in ("MULTIPLE_CHOICE", "CHECKBOX") and cached.get("chosen"):
                            matched_ids = [
                                opt["option_id"] for opt in options
                                if opt["value"] in cached["chosen"]
                            ]
                            if matched_ids:
                                answer_responses.append(self._format_response(
                                    part_id=part_id, q_type=q_type,
                                    chosen=matched_ids))
                                cache_hits += 1
                                continue

                if q_type in ("TEXT_REFLECT", "PLAIN_TEXT", "TEXT_EXACT_MATCH",
                              "NUMERIC", "MATH", "REGEX"):
                    if q.get("correct_answer"):
                        answer_responses.append(self._format_response(
                            part_id=part_id, q_type=q_type,
                            answer=q["correct_answer"]))
                    else:
                        unsolved_questions[part_id] = {
                            "Question": q["Question"], "Options": [],
                            "Type": q_type}

                elif q_type == "MULTIPLE_CHOICE":
                    known_correct_id = next(
                        (opt["option_id"] for opt in options if opt.get("correct") is True), None)
                    if known_correct_id:
                        answer_responses.append(self._format_response(
                            part_id=part_id, q_type="MULTIPLE_CHOICE",
                            chosen=[known_correct_id]))
                        continue
                    filtered_options = [
                        opt for opt in options if opt.get("correct") is not False]
                    if len(filtered_options) == 1:
                        answer_responses.append(self._format_response(
                            part_id=part_id, q_type="MULTIPLE_CHOICE",
                            chosen=[filtered_options[0]["option_id"]]))
                        continue
                    unsolved_questions[part_id] = {
                        "Question": q["Question"],
                        "Options": filtered_options,
                        "Type": "MULTIPLE_CHOICE"}

                elif q_type == "CHECKBOX":
                    all_resolved = all(
                        opt.get("correct") is not None for opt in options)
                    if all_resolved:
                        known_ids = [
                            opt["option_id"] for opt in options if opt.get("correct") is True]
                        answer_responses.append(self._format_response(
                            part_id=part_id, q_type="CHECKBOX",
                            chosen=known_ids))
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
                        "Type": "CHECKBOX"}
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
                logger.success(f"Cache hits: {cache_hits} questions solved from local DB")

            if unsolved_questions:
                connector = self._get_connector()

                if is_final_attempt:
                    BATCH_SIZE = len(unsolved_questions)
                    BATCH_DELAY = 0
                    active_prompt = SYSTEM_PROMPT + FINAL_ATTEMPT_PROMPT
                else:
                    BATCH_SIZE = 8
                    BATCH_DELAY = 8.0
                    active_prompt = SYSTEM_PROMPT

                all_responses = []
                question_ids = list(unsolved_questions.keys())
                total_batches = (len(question_ids) + BATCH_SIZE - 1) // BATCH_SIZE

                for i in range(0, len(question_ids), BATCH_SIZE):
                    batch_ids = question_ids[i:i + BATCH_SIZE]
                    batch = {qid: unsolved_questions[qid] for qid in batch_ids}

                    logger.info(
                        f"Sending batch {i // BATCH_SIZE + 1}/{total_batches} "
                        f"({len(batch)} questions) to LLM...")

                    try:
                        llm_result = connector.get_response(
                            batch, system_prompt=active_prompt,
                            response_schema=DEFAULT_RESPONSE_SCHEMA)
                        all_responses.extend(llm_result.get("responses", []))
                    except Exception as e:
                        logger.error(f"Batch {i // BATCH_SIZE + 1} failed: {e}")
                        continue

                    if i + BATCH_SIZE < len(question_ids) and BATCH_DELAY > 0:
                        time.sleep(BATCH_DELAY)

                if all_responses and is_final_attempt:
                    logger.info(f"Running self-verification on {len(all_responses)} answers...")
                    all_responses = self._verify_answers(
                        connector, unsolved_questions, all_responses)

                for ans in all_responses:
                    if ans["question_id"] not in unsolved_questions:
                        continue
                    answer_responses.append(self._format_response(
                        part_id=ans["question_id"],
                        q_type=unsolved_questions[ans["question_id"]]["Type"],
                        chosen=ans.get("chosen"),
                        answer=ans.get("answer")))
            else:
                logger.info("All questions resolved locally.")

            if not self.save_responses(answer_responses):
                logger.error("Could not save responses.")
                return False
            if not self.submit_draft():
                logger.error("Could not submit the assignment.")
                return False

            time.sleep(5.0)
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
                    f"Attempt {attempt_count} — Earned: {earned_grade:.1%} | Target: {target_grade:.1%}")

                if earned_grade >= target_grade:
                    logger.success(f"Passed on attempt {attempt_count}!")
                    return True

            random_delay()

    # ============================================================
    # CURRENT QUIZ MODE
    # ============================================================
    def solve_current(self) -> bool:
        logger.info("=" * 60)
        logger.info("CURRENT QUIZ MODE — solving this quiz only")
        logger.info(f"Course ID: {self.course_id} | Item ID: {self.item_id}")
        logger.info("=" * 60)

        state = self.get_state()
        allowed = state.get("allowedAction")
        logger.info(f"Allowed action: {allowed}")

        if state.get("outcome"):
            outcome = state["outcome"]
            logger.info(f"Current outcome: passed={outcome.get('isPassed')}, "
                        f"grade={outcome.get('earnedGrade', 0):.2%}")

        attempts = state.get("attempts", {})
        logger.info(f"Attempts made: {attempts.get('attemptsMade')} | "
                    f"remaining: {attempts.get('attemptsRemaining')} | "
                    f"allowed: {attempts.get('attemptsAllowed')}")

        previous_correctness = self._load_previous_correctness(state)
        if previous_correctness:
            correct_count = sum(1 for v in previous_correctness.values() if v == "CORRECT")
            incorrect_count = sum(1 for v in previous_correctness.values() if v == "INCORRECT")
            logger.info(f"Loaded correctness: {correct_count} correct, {incorrect_count} incorrect")

        if state.get("outcome") and state["outcome"].get("isPassed"):
            logger.success("Already passed this quiz!")
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

                if q_type in ("TEXT_REFLECT", "PLAIN_TEXT", "TEXT_EXACT_MATCH",
                              "NUMERIC", "MATH", "REGEX") and q.get("correct_answer"):
                    answer_responses.append(self._format_response(
                        part_id=part_id, q_type=q_type,
                        answer=q["correct_answer"]))
                    reused = True
                elif q_type == "MULTIPLE_CHOICE":
                    known_id = next(
                        (opt["option_id"] for opt in options if opt.get("correct") is True), None)
                    if known_id:
                        answer_responses.append(self._format_response(
                            part_id=part_id, q_type="MULTIPLE_CHOICE",
                            chosen=[known_id]))
                        reused = True
                elif q_type == "CHECKBOX":
                    known_ids = [opt["option_id"] for opt in options if opt.get("correct") is True]
                    if known_ids:
                        answer_responses.append(self._format_response(
                            part_id=part_id, q_type="CHECKBOX",
                            chosen=known_ids))
                        reused = True

                if not reused:
                    cached = lookup_cached(q["Question"], options)
                    if cached:
                        if q_type in ("TEXT_REFLECT", "PLAIN_TEXT", "TEXT_EXACT_MATCH",
                                      "NUMERIC", "MATH", "REGEX") and cached.get("answer"):
                            answer_responses.append(self._format_response(
                                part_id=part_id, q_type=q_type,
                                answer=cached["answer"]))
                            reused = True
                            cache_hits += 1
                        elif q_type in ("MULTIPLE_CHOICE", "CHECKBOX") and cached.get("chosen"):
                            matched_ids = [
                                opt["option_id"] for opt in options
                                if opt["value"] in cached["chosen"]
                            ]
                            if matched_ids:
                                answer_responses.append(self._format_response(
                                    part_id=part_id, q_type=q_type,
                                    chosen=matched_ids))
                                reused = True
                                cache_hits += 1

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
                    if q_type in ("TEXT_REFLECT", "PLAIN_TEXT", "TEXT_EXACT_MATCH",
                                  "NUMERIC", "MATH", "REGEX") and cached.get("answer"):
                        answer_responses.append(self._format_response(
                            part_id=part_id, q_type=q_type,
                            answer=cached["answer"]))
                        cache_hits += 1
                        continue
                    elif q_type in ("MULTIPLE_CHOICE", "CHECKBOX") and cached.get("chosen"):
                        matched_ids = [
                            opt["option_id"] for opt in options
                            if opt["value"] in cached["chosen"]
                        ]
                        if matched_ids:
                            answer_responses.append(self._format_response(
                                part_id=part_id, q_type=q_type,
                                chosen=matched_ids))
                            cache_hits += 1
                            continue

            if q_type in ("TEXT_REFLECT", "PLAIN_TEXT", "TEXT_EXACT_MATCH",
                          "NUMERIC", "MATH", "REGEX"):
                if q.get("correct_answer"):
                    answer_responses.append(self._format_response(
                        part_id=part_id, q_type=q_type,
                        answer=q["correct_answer"]))
                else:
                    unsolved_questions[part_id] = {
                        "Question": q["Question"], "Options": [],
                        "Type": q_type}

            elif q_type == "MULTIPLE_CHOICE":
                known_id = next(
                    (opt["option_id"] for opt in options if opt.get("correct") is True), None)
                if known_id:
                    answer_responses.append(self._format_response(
                        part_id=part_id, q_type="MULTIPLE_CHOICE",
                        chosen=[known_id]))
                    continue
                filtered_options = [
                    opt for opt in options if opt.get("correct") is not False]
                if len(filtered_options) == 1:
                    answer_responses.append(self._format_response(
                        part_id=part_id, q_type="MULTIPLE_CHOICE",
                        chosen=[filtered_options[0]["option_id"]]))
                    continue
                unsolved_questions[part_id] = {
                    "Question": q["Question"],
                    "Options": filtered_options,
                    "Type": "MULTIPLE_CHOICE"}

            elif q_type == "CHECKBOX":
                all_resolved = all(opt.get("correct") is not None for opt in options)
                if all_resolved:
                    known_ids = [opt["option_id"] for opt in options if opt.get("correct") is True]
                    answer_responses.append(self._format_response(
                        part_id=part_id, q_type="CHECKBOX", chosen=known_ids))
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
                    "Type": "CHECKBOX"}

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

        logger.info(f"Skipped (already correct): {skipped_correct}")
        logger.info(f"Cache hits: {cache_hits}")
        logger.info(f"To solve with LLM: {len(unsolved_questions)}")

        if unsolved_questions:
            connector = self._get_connector()
            active_prompt = SYSTEM_PROMPT + FINAL_ATTEMPT_PROMPT
            BATCH_SIZE = 8
            BATCH_DELAY = 8.0

            all_responses = []
            question_ids = list(unsolved_questions.keys())
            total_batches = (len(question_ids) + BATCH_SIZE - 1) // BATCH_SIZE

            for i in range(0, len(question_ids), BATCH_SIZE):
                batch_ids = question_ids[i:i + BATCH_SIZE]
                batch = {qid: unsolved_questions[qid] for qid in batch_ids}

                logger.info(
                    f"Sending batch {i // BATCH_SIZE + 1}/{total_batches} "
                    f"({len(batch)} questions) to LLM...")

                try:
                    llm_result = connector.get_response(
                        batch, system_prompt=active_prompt,
                        response_schema=DEFAULT_RESPONSE_SCHEMA)
                    all_responses.extend(llm_result.get("responses", []))
                except Exception as e:
                    logger.error(f"Batch failed: {e}")
                    continue

                if i + BATCH_SIZE < len(question_ids) and BATCH_DELAY > 0:
                    time.sleep(BATCH_DELAY)

            if all_responses:
                logger.info(f"Running self-verification on {len(all_responses)} answers...")
                all_responses = self._verify_answers(
                    connector, unsolved_questions, all_responses)

            for ans in all_responses:
                if ans["question_id"] not in unsolved_questions:
                    continue
                answer_responses.append(self._format_response(
                    part_id=ans["question_id"],
                    q_type=unsolved_questions[ans["question_id"]]["Type"],
                    chosen=ans.get("chosen"),
                    answer=ans.get("answer")))

        if not self.save_responses(answer_responses):
            logger.error("Could not save responses.")
            return False
        if not self.submit_draft():
            logger.error("Could not submit.")
            return False

        logger.success("Current quiz submitted!")
        time.sleep(5.0)

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
        """Load per-question correctness from Coursera feedback + local file."""
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
                logger.debug("No feedback parts available")
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
                    for rkey in ("textReflectResponse", "plainTextResponse",
                                 "textExactMatchResponse", "numericResponse",
                                 "mathResponse", "regexResponse"):
                        if rkey in part and part[rkey]:
                            fb_inner = part.get("feedback", {}) or {}
                            if fb_inner.get("correctness") == "CORRECT":
                                result[part_id] = "CORRECT"
                            break
                    continue

                response_key = None
                for k in ("multipleChoiceResponse", "checkboxResponse"):
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
                    if part_id in self.questions_data:
                        self.questions_data[part_id]["previous_correctness"] = "CORRECT"
                elif wrong_chosen > 0:
                    result[part_id] = "INCORRECT"

            correct_count = sum(1 for v in result.values() if v == "CORRECT")
            incorrect_count = sum(1 for v in result.values() if v == "INCORRECT")
            logger.info(f"Loaded correctness: {correct_count} correct, "
                        f"{incorrect_count} incorrect")

        except Exception as e:
            logger.debug(f"Could not load Coursera feedback: {e}")

        return result

    # ============================================================
    # SHARED API METHODS
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

        for question in questions:
            if not question["__typename"] in QUESTION_TYPE_MAP:
                continue
            if not question["__typename"] in WHITELISTED_QUESTION_TYPES:
                self.discarded_questions.append({
                    "questionId": question["partId"],
                    "questionType": QUESTION_TYPE_MAP[question["__typename"]][1],
                    "questionResponse": {
                        QUESTION_TYPE_MAP[question["__typename"]][0]:
                        deep_blank_model(MODEL_MAP[question["__typename"]])}})
                continue

            part_id = question["partId"]
            existing = self.questions_data.get(part_id, {})
            existing_options = existing.get("Options", [])
            existing_correctness = {}
            for opt in existing_options:
                if opt.get("correct") is not None:
                    existing_correctness[opt["value"]] = opt["correct"]

            options = []
            options_schema = question["questionSchema"].get("options") or []
            for option in options_schema:
                val = option["display"]["cmlValue"]
                options.append({
                    "option_id": option["optionId"],
                    "value": val,
                    "correct": existing_correctness.get(val, None)})

            questions_formatted[part_id] = {
                "Question": question["questionSchema"]["prompt"]["cmlValue"],
                "Options": options,
                "Type": QUESTION_TYPE_MAP[question["__typename"]][1]}

            if "correct_answer" in existing:
                questions_formatted[part_id]["correct_answer"] = existing["correct_answer"]
            if "incorrect_combinations" in existing:
                questions_formatted[part_id]["incorrect_combinations"] = existing["incorrect_combinations"]

        self.questions_data.update(questions_formatted)
        return questions_formatted

    # ============================================================
    # SAVE RESPONSES — FIXED (skips blank discarded questions)
    # ============================================================
    def save_responses(self, answer_responses: list) -> bool:
        """
        Save responses to Coursera draft.
        IMPORTANT: Skip discarded questions that have no real value —
        Coursera rejects them and blocks the entire save.
        """
        # Filter out blank discarded questions
        valid_discarded = []
        for dq in self.discarded_questions:
            resp = dq.get("questionResponse", {})
            has_value = False

            for key in ("multipleChoiceResponse", "checkboxResponse",
                        "textReflectResponse", "plainTextResponse",
                        "textExactMatchResponse", "numericResponse",
                        "mathResponse", "regexResponse"):
                if key not in resp:
                    continue
                inner = resp[key] or {}
                for v in inner.values():
                    if v not in (None, "", [], {}):
                        has_value = True
                        break
                if has_value:
                    break

            if has_value:
                valid_discarded.append(dq)

        payload_responses = [*answer_responses, *valid_discarded]

        logger.info(
            f"Saving {len(payload_responses)} responses "
            f"({len(answer_responses)} solved, {len(valid_discarded)} discarded-with-value)"
        )

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
                        "questionResponses": payload_responses
                    }
                },
                "query": SAVE_RESPONSES_QUERY
            }
        )

        if "Submission_SaveResponsesSuccess" in res.text:
            try:
                data = res.json()
                self.draft_id = (
                    data["data"]["Submission_SaveResponses"]
                    ["submissionState"]["attempts"]
                    ["inProgressAttempt"]["draft"]["id"]
                )
            except (KeyError, TypeError):
                pass
            return True

        # Second attempt: clean out any empty responses too
        cleaned_responses = []
        for resp in payload_responses:
            q_type = resp.get("questionType")
            q_resp = resp.get("questionResponse", {})

            if q_type == "REGEX":
                regex_resp = q_resp.get("regexResponse", {})
                if not regex_resp.get("answer"):
                    regex_resp["answer"] = ".*"

            if q_type == "NUMERIC":
                num_resp = q_resp.get("numericResponse", {})
                if not num_resp.get("answer"):
                    continue  # Skip empty numeric

            cleaned_responses.append(resp)

        logger.warning(f"Retrying save with {len(cleaned_responses)} cleaned responses...")

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
                        "questionResponses": cleaned_responses
                    }
                },
                "query": SAVE_RESPONSES_QUERY
            }
        )

        if "Submission_SaveResponsesSuccess" in res2.text:
            try:
                data = res2.json()
                self.draft_id = (
                    data["data"]["Submission_SaveResponses"]
                    ["submissionState"]["attempts"]
                    ["inProgressAttempt"]["draft"]["id"]
                )
            except (KeyError, TypeError):
                pass
            return True

        # Log detailed error for debugging
        logger.error("save_responses failed. Server response:")
        try:
            err_data = res2.json()
            logger.error(json.dumps(err_data, indent=2)[:1500])
        except Exception:
            logger.error(res2.text[:1500])

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
                logger.debug(f"Unexpected feedback response: {res}")
                return None
            if feedback is not None:
                parts = feedback.get("parts")
                if parts is not None and all(part.get("feedback") is not None for part in parts):
                    return feedback
            logger.warning(f"Feedback not ready (attempt {i + 1}/{max_retries})")
            random_delay()
        logger.warning("Feedback did not become available in time.")
        return None

    def _update_data_from_feedback(self, feedback_parts: list, submitted_responses: list) -> None:
        """Merge feedback into questions_data + store solved answers to global cache."""
        question_lookup = {}
        for part_id in self.questions_data:
            key = part_id.split("~")[-1]
            question_lookup[key] = part_id

        response_lookup = {}
        for resp in submitted_responses:
            response_key, val_key = TYPE_LOOKUP[resp["questionType"]]
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

            if question_type in ("TEXT_REFLECT", "PLAIN_TEXT", "TEXT_EXACT_MATCH",
                                 "NUMERIC", "MATH", "REGEX"):
                if correctness == "CORRECT" and not our_q.get("correct_answer"):
                    our_q["correct_answer"] = submitted_chosen
                    store_answer(
                        our_q["Question"], all_options,
                        answer=submitted_chosen)
                continue

            is_single = question_type == "MULTIPLE_CHOICE"
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

                if question_type == "MULTIPLE_CHOICE" and chosen_texts:
                    store_answer(
                        our_q["Question"], all_options,
                        chosen=[next(iter(chosen_texts))])
                elif question_type == "CHECKBOX" and chosen_texts:
                    store_answer(
                        our_q["Question"], all_options,
                        chosen=list(chosen_texts))

            elif correctness == "INCORRECT":
                if is_single and chosen_texts:
                    chosen_text = next(iter(chosen_texts))
                    for our_opt in all_options:
                        if our_opt["value"] == chosen_text:
                            our_opt["correct"] = False
                            break
                elif not is_single and chosen_texts:
                    our_q.setdefault("incorrect_combinations", [])
                    comb = sorted(list(chosen_texts))
                    if not any(existing_comb["combination"] == comb for existing_comb in our_q["incorrect_combinations"]):
                        our_q["incorrect_combinations"].append({
                            "combination": comb,
                            "score": outcome.get("score"),
                            "max_score": outcome.get("maxScore")})