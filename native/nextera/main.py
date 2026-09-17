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
    def __init__(self, course: str, llm: bool, mode: str = "complete"):
        self.user_id = None
        self.course_id = None
        self.base_url = BASE_URL
        self.session = httpx.Client(timeout=60.0, follow_redirects=True)
        self.session.headers.update(HEADERS)
        self.session.cookies.update(COOKIES)
        self.course = course
        self.llm = llm
        self.mode = mode
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
        # Special mode: shareable link (doesn't need course materials)
        if self.mode == "sharelink":
            self.generate_share_link()
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
        """Return SKIP_TYPES based on mode."""
        ALL = {"lecture", "supplement", "ungradedAssignment",
               "staffGraded", "discussionPrompt", "phasedPeer",
               "coach", "ungradedWidget", "ungradedLti"}

        if self.mode == "complete":
            # Videos + readings only
            return ALL - {"lecture", "supplement"}
        elif self.mode == "videos":
            return ALL - {"lecture"}
        elif self.mode == "readings":
            return ALL - {"supplement"}
        elif self.mode == "quizzes":
            # Practice quizzes only (ungradedAssignment)
            return ALL - {"ungradedAssignment"}
        elif self.mode == "graded":
            # Staff graded assignments only
            return ALL - {"staffGraded", "ungradedAssignment"}
        elif self.mode == "discussions":
            return ALL - {"discussionPrompt"}
        elif self.mode == "llm":
            # Everything except peer-graded
            return {"phasedPeer", "discussionPrompt"}
        else:
            return {"phasedPeer"}

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
                logger.warning(
                    f"Max iterations ({max_iterations}) reached. Stopping to avoid infinite loop.")
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

            # Separate assignment/discussion items (sequential) from others (concurrent)
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
                            logger.exception(
                                f"Error in processing item: {e}")
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
                f"[module:{module_id}] [item:{item_id}] Unknown/skipped item type: {item_type} - skipping.")

        return success

    def generate_share_link(self) -> None:
        """Generate shareable link for current assignment submission."""
        logger.info("Generating shareable link...")
        logger.info("Ye feature browser extension ke through kaam karta hai.")
        logger.info("Chrome mein Coursera assignment submission page kholo, phir 'Shareable Link' button dabao.")
        # Note: actual share link generation happens in content.js/browser

    def get_completed_items(self) -> set[str]:
        r = self.session.get(
            self.base_url +
            f"onDemandCoursesProgress.v1/{self.user_id}~{self.course_id}",
            params={"fields": "gradedAssignmentGroupProgress"}
        )

        if r.status_code != 200:
            logger.debug("Could not fetch course progress.")
            logger.debug(r.text)
            return set()

        data = r.json()
        elements = data.get("elements") or []
        if not elements:
            logger.debug("Course progress response has no elements.")
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
            logger.error(
                f"Failed to get session for widget {item_id}: {r.status_code}")
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
@click.option('--mode', default='complete', help="Mode: complete, llm, videos, readings, quizzes, graded, discussions, sharelink")
def main(slug: str, llm: bool, mode: str) -> None:
    nextera = Nextera(slug, llm, mode=mode)
    nextera.get_course()

if __name__ == '__main__':
    main()