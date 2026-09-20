import click
import httpx
from concurrent.futures import ThreadPoolExecutor, as_completed
from .config import fetch_browser_cookies, CONFIG_FILE, DEFAULT_CONFIG, BASE_URL, HEADERS, COOKIES
import json
from loguru import logger
from .assessment.solver import GradedSolver
from .discussion.solver import DiscussionPromptSolver
from .coach.solver import CoachSolver
from .watcher.watch import Watcher
from .session_utils import get_csrf_headers, random_delay


class Nextera(object):
    def __init__(self, course: str, llm: bool, mode: str = "complete", current_url: str = ""):
        self.user_id = None
        self.course_id = None
        self.base_url = BASE_URL
        self.session = httpx.Client(timeout=60.0, follow_redirects=True)
        self.session.headers.update(HEADERS)
        self.session.cookies.update(COOKIES)
        self.course = course
        self.llm = llm
        self.mode = mode
        self.current_url = current_url
        self.failed_items = set()
        if not self.get_userid():
            self.refresh_cookies()
            if not self.get_userid():
                logger.error(
                    "Cookies are invalid. Log into Coursera in your browser, close it, and retry.")
                raise SystemExit

    def refresh_cookies(self):
        logger.warning("Session expired — re-fetching cookies from browser...")
        cookies = fetch_browser_cookies()
        if not cookies:
            return
        self.session.cookies.clear()
        self.session.cookies.update(cookies)
        cfg = json.loads(CONFIG_FILE.read_text()
                         ) if CONFIG_FILE.exists() else DEFAULT_CONFIG.copy()
        cfg["cookies"] = cookies
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2))

    def get_userid(self) -> bool:
        r = self.session.get(
            self.base_url + "adminUserPermissions.v1?q=my").json()
        try:
            self.user_id = r["elements"][0]["id"]
            logger.info("User ID: " + self.user_id)
        except KeyError:
            if r.get("errorCode"):
                logger.error("Error Encountered: " + r["errorCode"])
            return False
        return True

    def get_course(self) -> None:
        if self.mode == "sharelink":
            self.generate_share_link()
            return

        if self.mode == "current":
            self.solve_current_quiz()
            return

        r = self.get_course_materials()
        self.course_id = r["elements"][0]["id"]
        all_items = r["linked"]["onDemandCourseMaterialItems.v2"]

        logger.info("Course ID: " + self.course_id)
        logger.info("Number of Modules: " +
                    str(len(r["linked"]["onDemandCourseMaterialModules.v1"])))
        logger.info("Total items: " + str(len(all_items)))

        self.process_items(all_items)

    def get_course_materials(self) -> dict:
        r = self.session.get(self.base_url + f"onDemandCourseMaterials.v2/", params={
            "q": "slug",
            "slug": self.course,
            "includes": "modules,lessons,passableItemGroups,passableItemGroupChoices,passableLessonElements,items,"
                        "tracks,gradePolicy,gradingParameters,embeddedContentMapping",
            "fields": "moduleIds,onDemandCourseMaterialModules.v1(name,slug,description,timeCommitment,lessonIds,"
                      "optional,learningObjectives),onDemandCourseMaterialLessons.v1(name,slug,timeCommitment,"
                      "elementIds,optional,trackId),onDemandCourseMaterialPassableItemGroups.v1(requiredPassedCount,"
                      "passableItemGroupChoiceIds,trackId),onDemandCourseMaterialPassableItemGroupChoices.v1(name,"
                      "description,itemIds),onDemandCourseMaterialPassableLessonElements.v1(gradingWeight,"
                      "isRequiredForPassing),onDemandCourseMaterialItems.v2(name,originalName,slug,timeCommitment,"
                      "contentSummary,isLocked,lockableByItem,itemLockedReasonCode,trackId,lockedStatus,itemLockSummary,"
                      "customDisplayTypenameOverride),onDemandCourseMaterialTracks.v1(passablesCount),"
                      "onDemandGradingParameters.v1(gradedAssignmentGroups),"
                      "contentAtomRelations.v1(embeddedContentSourceCourseId,subContainerId)",
            "showLockedItems": True
        })

        if r.status_code != 200:
            logger.error("Please check if you are enrolled in the course!")
            raise SystemExit

        return r.json()

    def _get_skip_types(self) -> set:
        ALL = {"lecture", "supplement", "ungradedAssignment",
               "staffGraded", "discussionPrompt", "phasedPeer",
               "coach", "ungradedWidget", "ungradedLti"}

        if self.mode == "complete":
            return ALL - {"lecture", "supplement"}
        elif self.mode == "llm":
            return ALL - {"ungradedAssignment", "staffGraded", "discussionPrompt"}
        elif self.mode == "videos":
            return ALL - {"lecture"}
        elif self.mode == "readings":
            return ALL - {"supplement"}
        elif self.mode == "quizzes":
            return ALL - {"ungradedAssignment"}
        elif self.mode == "graded":
            return ALL - {"staffGraded", "ungradedAssignment"}
        elif self.mode == "discussions":
            return ALL - {"discussionPrompt"}
        elif self.mode == "sharelink":
            return ALL
        elif self.mode == "current":
            return ALL
        else:
            return {"phasedPeer"}

    def _extract_slug_from_url(self, url: str) -> str | None:
        """
        Extract item slug from various Coursera URL formats:
          /learn/<course>/quiz/<slug>/...
          /learn/<course>/assignment/<slug>/...
          /learn/<course>/assignment-submission/<sub>/<slug>/...
          /learn/<course>/exam/<slug>/...
          /learn/<course>/supplement/<slug>/...
        """
        if not url:
            return None

        url = url.split("?")[0].split("#")[0]
        parts = [p for p in url.split("/") if p]

        try:
            learn_idx = parts.index("learn")
        except ValueError:
            return None

        if learn_idx + 2 >= len(parts):
            return None

        keywords = ("quiz", "assignment", "assignment-submission",
                    "exam", "supplement", "lecture", "reading")

        for i in range(learn_idx + 2, len(parts)):
            if parts[i] in keywords:
                if parts[i] == "assignment-submission":
                    if i + 2 < len(parts):
                        return parts[i + 2]
                    elif i + 1 < len(parts):
                        return parts[i + 1]
                else:
                    if i + 1 < len(parts):
                        return parts[i + 1]

        return None

    def solve_current_quiz(self) -> None:
        """Solve only the currently-open quiz OR assignment in browser."""
        logger.info("=" * 60)
        logger.info("CURRENT QUIZ MODE")
        logger.info(f"Current URL: {self.current_url}")
        logger.info("=" * 60)

        item_slug = self._extract_slug_from_url(self.current_url)
        logger.info(f"Parsed item slug: {item_slug}")

        r = self.get_course_materials()
        self.course_id = r["elements"][0]["id"]
        all_items = r["linked"]["onDemandCourseMaterialItems.v2"]
        logger.info(f"Course ID: {self.course_id}")
        logger.info(f"Total items in course: {len(all_items)}")

        target_item = None
        if item_slug:
            for item in all_items:
                if item.get("slug") == item_slug:
                    target_item = item
                    logger.info(f"Exact match: {item['name']} ({item['contentSummary']['typeName']})")
                    break

            if not target_item:
                for item in all_items:
                    item_slug_field = item.get("slug") or ""
                    if item_slug in item_slug_field or item_slug_field in item_slug:
                        target_item = item
                        logger.info(f"Fuzzy match: {item['name']} ({item['contentSummary']['typeName']})")
                        break

        if not target_item:
            logger.warning("No slug match — trying fallback")
            completed = self.get_completed_items()
            assignment_types = {"ungradedAssignment", "staffGraded"}
            for item in all_items:
                if item["contentSummary"]["typeName"] in assignment_types \
                        and item["id"] not in completed:
                    target_item = item
                    logger.info(f"Fallback match: {item['name']} ({item['contentSummary']['typeName']})")
                    break

        if not target_item:
            logger.error("FOUND NO ASSIGNMENT/QUIZ TO SOLVE")
            return

        item_type = target_item["contentSummary"]["typeName"]
        logger.info(f"TARGET: [{item_type}] {target_item['name']}")
        logger.info(f"Item ID: {target_item['id']}")

        GradedSolver(
            self.session, self.course_id, target_item["id"]
        ).solve_current()

    def process_items(self, all_items: list[dict]) -> None:
        total = len(all_items)
        iteration_count = 0
        max_iterations = 100

        SKIP_TYPES = self._get_skip_types()

        logger.info(f"Mode: {self.mode}")
        logger.info(f"Skip types: {SKIP_TYPES}")

        while True:
            iteration_count += 1
            if iteration_count > max_iterations:
                logger.warning(f"Max iterations ({max_iterations}) reached.")
                break

            completed = self.get_completed_items()

            try:
                fresh_data = self.get_course_materials()
                current_items = fresh_data["linked"]["onDemandCourseMaterialItems.v2"]
            except SystemExit:
                current_items = all_items

            pending_items = [
                item for item in current_items
                if item["id"] not in completed
                and item["id"] not in self.failed_items
                and item["contentSummary"]["typeName"] not in SKIP_TYPES
            ]

            if not pending_items:
                skipped_count = len([
                    i for i in current_items
                    if i["contentSummary"]["typeName"] in SKIP_TYPES
                ])
                logger.info(
                    f"Finished: {len(completed)}/{total} completed, "
                    f"{len(self.failed_items)} failed, "
                    f"{skipped_count} skipped."
                )
                break

            unlocked_items = [
                item for item in pending_items
                if not item.get("isLocked", False)
            ]
            if not unlocked_items:
                logger.info(
                    f"Finished: {total - len(pending_items)}/{total} completed, "
                    f"{len(pending_items)} still locked/pending."
                )
                break

            sequential_types = {"discussionPrompt", "ungradedAssignment",
                                "staffGraded", "phasedPeer"}

            concurrent_items = []
            sequential_items = []
            for item in unlocked_items:
                if item["contentSummary"]["typeName"] in sequential_types:
                    sequential_items.append(item)
                else:
                    concurrent_items.append(item)

            if concurrent_items:
                with ThreadPoolExecutor(max_workers=min(4, len(concurrent_items))) as executor:
                    futures = {
                        executor.submit(self.process_item, item): item
                        for item in concurrent_items
                    }
                    for future in as_completed(futures):
                        item = futures[future]
                        try:
                            success = future.result()
                            if not success:
                                self.failed_items.add(item["id"])
                        except Exception as e:
                            logger.exception(f"Error in processing item: {e}")
                            self.failed_items.add(item["id"])
                continue

            if sequential_items:
                item = sequential_items[0]
                try:
                    success = self.process_item(item)
                    if not success:
                        self.failed_items.add(item["id"])
                    random_delay(4.0, 8.0)
                except Exception as e:
                    logger.exception(f"Error in processing item: {e}")
                    self.failed_items.add(item["id"])
                continue

    def process_item(self, item: dict) -> bool:
        item_type = item["contentSummary"]["typeName"]
        module_id = item.get('moduleId', 'unknown')
        item_id = item['id']
        logger.info(
            f"[module:{module_id}] [item:{item_id}] Processing {item['name']}")

        success = False
        if item_type == "lecture":
            success = self.watch_item(item, self.get_video_metadata(item_id))
        elif item_type == "supplement":
            success = self.read_item(item_id)
        elif item_type in {"ungradedAssignment", "staffGraded"} and self.llm:
            success = GradedSolver(
                self.session, self.course_id, item_id).solve()
        elif item_type == "discussionPrompt" and self.llm:
            success = DiscussionPromptSolver(
                self.session, self.user_id, self.course_id, item_id).solve()
        elif item_type == "coach":
            success = CoachSolver(
                self.session, self.user_id, self.course_id, item_id).solve()
        elif item_type == "ungradedWidget":
            success = self.ungraded_widget_item(item_id)
        elif item_type == "ungradedLti":
            success = self.ungraded_lti_item(item_id)
        else:
            logger.warning(
                f"[module:{module_id}] [item:{item_id}] Unknown/skipped type: {item_type}")

        return success

    def generate_share_link(self) -> None:
        logger.info("Shareable link feature browser extension ke through kaam karta hai.")
        logger.info("Chrome mein assignment submission page kholo, phir 'Shareable Link' button dabao.")

    def get_completed_items(self) -> set[str]:
        r = self.session.get(
            self.base_url +
            f"onDemandCoursesProgress.v1/{self.user_id}~{self.course_id}",
            params={"fields": "gradedAssignmentGroupProgress"}
        )

        if r.status_code != 200:
            logger.debug("Could not fetch course progress.")
            return set()

        data = r.json()
        elements = data.get("elements") or []
        if not elements:
            return set()

        items = elements[0].get("items", {})
        return {
            item_id
            for item_id, progress in items.items()
            if progress.get("progressState") == "Completed"
        }

    def get_video_metadata(self, item_id: str) -> dict:
        r = self.session.get(self.base_url + f"onDemandLectureVideos.v1/{self.course_id}~{item_id}", params={
            "includes": "video",
            "fields": "disableSkippingForward,startMs,endMs"
        }).json()

        return {"can_skip": not r["elements"][0]["disableSkippingForward"],
                "tracking_id": r["linked"]["onDemandVideos.v1"][0]["id"]}

    def watch_item(self, item: dict, metadata: dict) -> bool:
        watcher = Watcher(self.session, item, metadata,
                          self.user_id, self.course, self.course_id)
        return watcher.watch_item()

    def read_item(self, item_id) -> bool:
        r = self.session.post(self.base_url + "onDemandSupplementCompletions.v1",
                              headers=get_csrf_headers(self.session),
                              json={
                                  "courseId": self.course_id,
                                  "itemId": item_id,
                                  "userId": int(self.user_id)
                              })
        return "Completed" in r.text

    def ungraded_widget_item(self, item_id) -> bool:
        r = self.session.get(
            self.base_url + f"onDemandWidgetSessions.v1/{self.user_id}~{self.course_id}~{item_id}",
            params={"fields": "session,sessionId"}
        )
        if r.status_code != 200:
            logger.error(f"Failed to get session for widget {item_id}")
            return False

        try:
            session_id = r.json()["elements"][0]["sessionId"]
        except (KeyError, IndexError):
            logger.error(f"Could not parse sessionId for widget {item_id}")
            return False

        res = self.session.put(
            self.base_url + f"onDemandWidgetProgress.v1/{self.user_id}~{self.course_id}~{item_id}",
            headers=get_csrf_headers(self.session),
            json={
                "sessionId": session_id,
                "progressState": "Completed"
            }
        )
        return 200 <= res.status_code < 300

    def ungraded_lti_item(self, item_id) -> bool:
        r = self.session.post(
            self.base_url + "rest/v1/lti/ungradedLaunches",
            headers=get_csrf_headers(self.session),
            json={
                "courseId": self.course_id,
                "itemId": item_id,
                "learnerId": int(self.user_id),
                "markItemCompleted": True
            }
        )
        return 200 <= r.status_code < 300


@logger.catch
@click.command()
@click.argument('slug')
@click.option('--llm', is_flag=True, help="Whether to use an LLM to solve graded assignments.")
@click.option('--mode', default='complete',
              help="Mode: complete, llm, videos, readings, quizzes, graded, discussions, sharelink, current")
def main(slug: str, llm: bool, mode: str) -> None:
    nextera = Nextera(slug, llm, mode=mode)
    nextera.get_course()


if __name__ == '__main__':
    main()