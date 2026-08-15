package com.qiuzhao.flashcards.data.remote.v25

import com.qiuzhao.flashcards.domain.v25.V25ApiKeyState
import com.qiuzhao.flashcards.domain.v25.V25AvatarKey
import com.qiuzhao.flashcards.domain.v25.V25BrowseOrder
import com.qiuzhao.flashcards.domain.v25.V25CardType
import com.qiuzhao.flashcards.domain.v25.V25ContentDifficulty
import com.qiuzhao.flashcards.domain.v25.V25CoverageMode
import com.qiuzhao.flashcards.domain.v25.V25DeletionBatchStatus
import com.qiuzhao.flashcards.domain.v25.V25Difficulty
import com.qiuzhao.flashcards.domain.v25.V25InternalStage
import com.qiuzhao.flashcards.domain.v25.V25MasteryFilter
import com.qiuzhao.flashcards.domain.v25.V25ProjectStatus
import com.qiuzhao.flashcards.domain.v25.V25PublicationState
import com.qiuzhao.flashcards.domain.v25.V25RewriteStatus
import com.qiuzhao.flashcards.domain.v25.V25TaskStatus
import java.time.Instant
import java.time.LocalDate
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Serialization fixtures taken verbatim from the committed backend OpenAPI examples
 * (openapi.yaml schemas + structure-contract.md resource models): one fixture per payload
 * family the V2.5 boundary consumes, including the failure envelope. These tests lock the
 * wire-to-domain mapping — enum names, snake_case fields, ISO timestamps, nullable fields —
 * so a backend contract change breaks this file before it reaches a single UI.
 */
class V25SerializationTest {

    @Test
    fun `auth user payload maps to the typed profile`() {
        val user = parseAuthUser(meBody().getJSONObject("user"))
        assertEquals("u-1", user.userId)
        assertEquals("alice", user.username)
        assertEquals("alice@example.com", user.email)
        assertEquals(V25AvatarKey.mood_03, user.avatarKey)
        assertEquals(Instant.parse("2026-08-14T09:00:00Z"), user.createdAt)
    }

    @Test
    fun `preferences payload maps ratios goal timezone and nullable current project`() {
        val prefs = parseUserPreferences(JSONObject(preferencesBody()))
        assertEquals(V25CoverageMode.BALANCED, prefs.defaultCoverageMode)
        assertEquals(40, prefs.difficultyRatio.basic)
        assertEquals(40, prefs.difficultyRatio.understanding)
        assertEquals(20, prefs.difficultyRatio.deepQuestion)
        assertEquals(50, prefs.dailyLearningGoal)
        assertEquals("Asia/Shanghai", prefs.learningTimezone)
        assertNull(prefs.currentProjectId)
        assertEquals(Instant.parse("2026-08-14T09:00:00Z"), prefs.updatedAt)
    }

    @Test
    fun `preferences payload keeps a set current project id`() {
        val prefs = parseUserPreferences(JSONObject(preferencesBody().replace("\"current_project_id\": null", "\"current_project_id\": \"p-1\"")))
        assertEquals("p-1", prefs.currentProjectId)
    }

    @Test
    fun `empty items payload is an empty list not a failure`() {
        val items = requiredArray(JSONObject("""{"items": []}"""), "items")
        assertTrue(items.isEmpty())
    }

    @Test
    fun `project payload maps the pdf file chapters and derived counts`() {
        val project = parseLearningProject(JSONObject(projectBody()))
        assertEquals("p-1", project.projectId)
        assertEquals("线性代数", project.name)
        assertEquals(V25ProjectStatus.READY, project.status)
        assertEquals(1, project.chapterCount)
        assertEquals(2, project.deckCount)
        assertEquals(3, project.taskCount)
        assertEquals("f-1", project.file.id)
        assertEquals("linear.pdf", project.file.name)
        assertEquals(1, project.file.chapters.size)
        assertEquals("c-1", project.file.chapters[0].id)
        assertEquals("第一章 矩阵", project.file.chapters[0].name)
        assertEquals(1, project.file.chapters[0].startPage)
        assertEquals(20, project.file.chapters[0].endPage)
        assertEquals(Instant.parse("2026-08-14T09:00:00Z"), project.createdAt)
        assertEquals(Instant.parse("2026-08-14T10:00:00Z"), project.updatedAt)
    }

    @Test
    fun `project payload maps the parsing status and a file without chapters`() {
        val project = parseLearningProject(JSONObject(projectBody()
            .replace("\"status\": \"READY\"", "\"status\": \"PARSING\"")
            .replace("\"chapters\": [{\"chapter_id\": \"c-1\", \"name\": \"第一章 矩阵\", \"start_page\": 1, \"end_page\": 20}]", "\"chapters\": null")))
        assertEquals(V25ProjectStatus.PARSING, project.status)
        assertTrue(project.file.chapters.isEmpty())
    }

    @Test
    fun `task payload maps status stage config and nullables`() {
        val task = parseGenerationTask(JSONObject(taskBody()))
        assertEquals("t-1", task.taskId)
        assertEquals("p-1", task.projectId)
        assertEquals("f-1", task.fileId)
        assertEquals("d-1", task.deckId)
        assertNull(task.retryOfTaskId)
        assertEquals(V25TaskStatus.GENERATING, task.status)
        assertEquals(V25InternalStage.SCORING, task.internalStage)
        assertEquals(1, task.selectedChapters.size)
        assertEquals(V25CoverageMode.EXTENSIVE, task.generationConfig.coverageMode)
        assertEquals(40, task.generationConfig.difficultyRatio.basic)
        assertEquals("", task.generationConfig.customRequirements)
        assertTrue(task.sampleCards.isEmpty())
        assertNull(task.sampleConfigHash)
        assertNull(task.sampleConfirmedAt)
        assertEquals(3, task.generatedCardCount)
        assertNull(task.errorCode)
        assertNull(task.failureStage)
        assertEquals(Instant.parse("2026-08-14T09:01:00Z"), task.startedAt)
        assertNull(task.endedAt)
    }

    @Test
    fun `task payload maps confirmed samples per difficulty tier`() {
        val task = parseGenerationTask(JSONObject(taskBody()
            .replace("\"sample_cards\": null", """ "sample_cards": [{"card_id": "s-1", "front": "什么是矩阵？", "back": "数表", "card_type": "QUESTION", "target_difficulty": "BASIC"}] """)
            .replace("\"status\": \"GENERATING\"", "\"status\": \"AWAITING_SAMPLE_CONFIRMATION\"")
            .replace("\"internal_stage\": \"SCORING\"", "\"internal_stage\": null")
            .replace("\"sample_config_hash\": null", "\"sample_config_hash\": \"abc123\"")
            .replace("\"sample_confirmed_at\": null", "\"sample_confirmed_at\": \"2026-08-14T09:02:00Z\"")))
        assertEquals(V25TaskStatus.AWAITING_SAMPLE_CONFIRMATION, task.status)
        assertNull(task.internalStage)
        assertEquals(1, task.sampleCards.size)
        assertEquals("什么是矩阵？", task.sampleCards[0].front)
        assertEquals(V25Difficulty.BASIC, task.sampleCards[0].targetDifficulty)
        assertEquals(V25CardType.QUESTION, task.sampleCards[0].cardType)
        assertEquals("abc123", task.sampleConfigHash)
        assertEquals(Instant.parse("2026-08-14T09:02:00Z"), task.sampleConfirmedAt)
    }

    @Test
    fun `deck payload maps counts and mastery ratio`() {
        val deck = parseDeck(JSONObject(deckBody()))
        assertEquals("d-1", deck.deckId)
        assertEquals("线性代数", deck.name)
        assertEquals("p-1", deck.projectId)
        assertEquals(42, deck.cardCount)
        assertEquals(7, deck.dueCount)
        assertEquals(12, deck.masteredCards)
        assertEquals(30, deck.reviewCount)
        assertEquals(12f / 42f, deck.masteryRatio!!, 0.0001f)
    }

    @Test
    fun `deck payload with zero cards keeps a zero ratio`() {
        val deck = parseDeck(JSONObject(deckBody()
            .replace("\"card_count\": 42", "\"card_count\": 0")
            .replace("\"due_count\": 7", "\"due_count\": 0")
            .replace("\"mastered_card_count\": 12", "\"mastered_card_count\": 0")
            .replace("\"review_count\": 30", "\"review_count\": 0")
            .replace("\"mastery_ratio\": 0.2857143", "\"mastery_ratio\": 0")))
        assertEquals(0f, deck.masteryRatio!!, 0.0001f)
    }

    @Test
    fun `deck payload with a null project is an independent deck`() {
        val deck = parseDeck(JSONObject(deckBody().replace("\"project_id\": \"p-1\"", "\"project_id\": null")))
        assertNull(deck.projectId)
    }

    @Test
    fun `card payload maps position chapter source task and state`() {
        val card = parseCard(JSONObject(cardBody()))
        assertEquals("c-1", card.cardId)
        assertEquals("d-1", card.deckId)
        assertEquals("什么是矩阵？", card.front)
        assertEquals("按行列排列的数表", card.back)
        assertEquals(V25CardType.QUESTION, card.cardType)
        assertEquals(V25Difficulty.BASIC, card.targetDifficulty)
        assertEquals(3, card.position)
        assertEquals("ch-1", card.chapterId)
        assertEquals("t-1", card.sourceTaskId)
        assertEquals(V25PublicationState.PUBLISHED, card.publicationState)
        assertEquals(3, card.version)
    }

    @Test
    fun `card payload without publication state defaults to PUBLISHED`() {
        val card = parseCard(JSONObject(cardBody().replace("\"publication_state\": \"PUBLISHED\", ", "")))
        assertEquals(V25PublicationState.PUBLISHED, card.publicationState)
    }

    @Test
    fun `card version parses vN and iso timestamp wire values`() {
        assertEquals(3, parseVersion("v3"))
        assertEquals(0, parseVersion(null))
        // V2.5 writes version=now (ISO 8601 UTC string); the boundary models it as an Int
        // marker that only serves cache-refresh change detection (updated_at is authoritative).
        assertEquals(
            Instant.parse("2026-08-14T10:00:00Z").epochSecond.toInt(),
            parseVersion("2026-08-14T10:00:00Z"),
        )
        assertEquals(0, parseVersion("garbage"))
    }

    @Test
    fun `validation error envelope maps to a coded failure`() {
        val error = parseError(validationErrorBody())!!
        assertEquals("VALIDATION_ERROR", error.code)
        assertEquals("error.validation", error.localizationKey)
        assertEquals("比例合计必须为 100", error.message)
    }

    @Test
    fun `expired auth envelope maps to the auth failure code`() {
        val error = parseError(expiredAuthBody())!!
        assertEquals("AUTH_INVALID", error.code)
        assertEquals("auth.invalid", error.localizationKey)
    }

    @Test
    fun `conflict envelope passes the stable code through`() {
        val error = parseError(conflictBody())!!
        assertEquals("CARD_VERSION_CONFLICT", error.code)
    }

    @Test
    fun `error envelope without an error object yields no code`() {
        assertNull(parseError("""{"detail": "not the contract shape"}"""))
    }

    @Test
    fun `deletion batch payload maps the undo window`() {
        val batch = parseDeletionBatch(JSONObject(deletionBatchBody()))
        assertEquals("b-1", batch.deleteBatchId)
        assertEquals(listOf("c-1", "c-2"), batch.cardIds)
        assertEquals(Instant.parse("2026-08-15T09:00:10Z"), batch.undoUntil)
        assertEquals(V25DeletionBatchStatus.PENDING, batch.status)
        assertEquals(Instant.parse("2026-08-15T09:00:00Z"), batch.createdAt)
    }

    @Test
    fun `rewrite preview payload maps the two-stage contract`() {
        val preview = parseRewritePreview(JSONObject(rewritePreviewBody()))
        assertEquals("r-1", preview.rewriteId)
        assertEquals("c-1", preview.cardId)
        assertEquals("v3", preview.baseCardVersion)
        assertEquals("矩阵的秩是什么？", preview.front)
        assertEquals(V25CardType.QUESTION, preview.cardType)
        assertEquals(V25Difficulty.UNDERSTANDING, preview.targetDifficulty)
        assertEquals("强调直觉", preview.customRequirements)
        assertEquals(V25RewriteStatus.PENDING, preview.status)
        assertEquals(Instant.parse("2026-08-16T09:00:00Z"), preview.expiresAt)
    }

    @Test
    fun `stats dashboard maps weekly buckets goal and nullable rates`() {
        val stats = parseStatsDashboard(JSONObject(statsBody()))
        assertTrue(stats.hasData)
        assertEquals(7, stats.weeklyActivity.size)
        assertEquals(5, stats.weeklyActivity[0].ratingCount)
        assertEquals(1, stats.weeklyActivity[6].ratingCount)
        assertEquals(21, stats.weeklyTotalRatings)
        assertEquals(7, stats.weeklyGoalCompleted)
        assertNull(stats.weeklyChangeRate)
        assertEquals(350, stats.weeklyGoal)
        assertEquals(0.02f, stats.weeklyGoalRate!!, 0.0001f)
        assertEquals(0.8f, stats.recallAccuracy!!, 0.0001f)
        assertEquals(0.75f, stats.firstAttemptAccuracy!!, 0.0001f)
        assertEquals(0.82f, stats.retentionRate!!, 0.0001f)
        assertEquals(3, stats.streakDays)
        assertEquals(12, stats.masteredCards)
        assertEquals(Instant.parse("2026-08-15T12:00:00Z"), stats.updatedAt)
        // The dashboard wire carries no per-scope progress summaries yet (V25-STATS-FR-05);
        // the boundary keeps the honest empty list instead of fabricating numbers.
        assertTrue(stats.progress.isEmpty())
    }

    @Test
    fun `stats activity dates derive from the timezone aware week start`() {
        val stats = parseStatsDashboard(JSONObject(statsBody()))
        // The wire start is UTC ("...T16:00:00.000Z"): it must be projected through the
        // dashboard's timezone field (Asia/Shanghai) to land on the account-local Monday
        // 2026-08-10; projecting the raw UTC instant instead would yield 2026-08-09.
        assertEquals(LocalDate.parse("2026-08-10"), stats.weeklyActivity[0].studyDate)
        assertEquals(LocalDate.parse("2026-08-16"), stats.weeklyActivity[6].studyDate)
    }

    @Test
    fun `stats activity dates hold for a UTC bucketing timezone`() {
        val stats = parseStatsDashboard(JSONObject(statsBody()
            .replace("2026-08-09T16:00:00.000Z", "2026-08-10T00:00:00.000Z")
            .replace("2026-08-15T15:59:59.000Z", "2026-08-16T23:59:59.000Z")
            .replace("\"timezone\": \"Asia/Shanghai\"", "\"timezone\": \"UTC\"")))
        assertEquals(LocalDate.parse("2026-08-10"), stats.weeklyActivity[0].studyDate)
        assertEquals(LocalDate.parse("2026-08-16"), stats.weeklyActivity[6].studyDate)
    }

    @Test
    fun `stats dashboard empty state maps without fabrication`() {
        val stats = parseStatsDashboard(JSONObject(statsBody()
            .replace("\"has_data\": true", "\"has_data\": false")
            .replace("\"recall_accuracy\": 0.8", "\"recall_accuracy\": null")
            .replace("\"first_answer_accuracy\": 0.75", "\"first_answer_accuracy\": null")
            .replace("\"retention_rate\": 0.82", "\"retention_rate\": null")))
        assertTrue(!stats.hasData)
        assertNull(stats.recallAccuracy)
        assertNull(stats.firstAttemptAccuracy)
        assertNull(stats.retentionRate)
    }

    @Test
    fun `multipart file names are escaped before framing`() {
        // Header-safe names pass through untouched; quotes and line breaks that could
        // break the multipart frame are neutralised.
        assertEquals("report_2026.pdf", escapeMultipartFileName("report_2026.pdf"))
        assertEquals("a%22b.pdf", escapeMultipartFileName("a\"b.pdf"))
        assertFalse(escapeMultipartFileName("x\r\ny.pdf").contains("\r"))
        assertFalse(escapeMultipartFileName("x\r\ny.pdf").contains("\n"))
    }

    @Test
    fun `today plan empty state has a null current project`() {
        val plan = parseTodayPlan(JSONObject(todayPlanEmptyBody()))
        assertEquals("Asia/Shanghai", plan.learningTimezone)
        assertEquals(LocalDate.parse("2026-08-15"), plan.studyDate)
        assertNull(plan.currentProject)
        assertEquals(50, plan.dailyGoal)
        assertEquals(0, plan.completedCount)
        assertEquals(0, plan.dueCount)
        assertEquals(0, plan.planRemaining)
        assertEquals(0, plan.backlogCount)
        assertTrue(plan.cards.isEmpty())
    }

    @Test
    fun `plan card marks review-state NEW as isNew and keeps the summary project`() {
        val plan = parseTodayPlan(JSONObject(todayPlanBody()))
        assertEquals("p-1", plan.currentProject!!.projectId)
        assertEquals("线性代数", plan.currentProject!!.name)
        assertEquals(2, plan.dueCount)
        assertEquals(47, plan.planRemaining)
        assertEquals(1, plan.cards.size)
        val planCard = plan.cards[0]
        assertEquals("c-1", planCard.card.cardId)
        assertTrue(planCard.isNew)
        assertEquals("NEW", planCard.reviewState!!.state)
        assertEquals(Instant.parse("2026-08-15T09:00:00Z"), planCard.reviewState!!.due)
    }

    @Test
    fun `plan card with a learned review state is not new`() {
        val plan = parseTodayPlan(JSONObject(todayPlanBody().replace("\"state\": \"NEW\"", "\"state\": \"REVIEW\"")))
        assertTrue(!plan.cards[0].isNew)
    }

    @Test
    fun `review queue item maps the card and its fsrs state`() {
        val item = parseReviewCard(JSONObject(reviewQueueItemBody()))
        assertEquals("c-1", item.card.cardId)
        assertEquals("REVIEW", item.reviewState!!.state)
        assertEquals(Instant.parse("2026-08-20T09:00:00Z"), item.reviewState!!.due)
    }

    @Test
    fun `rating result maps the updated fsrs state and study date`() {
        val result = parseRatingResult(JSONObject(ratingResultBody()))
        assertEquals("LEARNING", result.reviewState.state)
        assertEquals(Instant.parse("2026-08-15T09:10:00Z"), result.reviewState.due)
        assertEquals(LocalDate.parse("2026-08-15"), result.studyDate)
    }

    @Test
    fun `api key status maps UNKNOWN to UNSET with a null mask`() {
        val status = parseApiKeyStatus(JSONObject(apiKeyUnknownBody()))
        assertEquals(V25ApiKeyState.UNSET, status.state)
        assertNull(status.maskedKey)
    }

    @Test
    fun `api key status maps a masked key and available state`() {
        val status = parseApiKeyStatus(JSONObject(apiKeyAvailableBody()))
        assertEquals(V25ApiKeyState.AVAILABLE, status.state)
        assertEquals("sk-****1234", status.maskedKey)
    }

    @Test
    fun `malformed success payload throws so the repository can map INVALID_RESPONSE`() {
        assertThrows(IllegalArgumentException::class.java) {
            parseLearningProject(JSONObject("""{"project_id": "p-1"}"""))
        }
        assertThrows(IllegalArgumentException::class.java) {
            parseCard(JSONObject(cardBody().replace("\"card_type\": \"QUESTION\"", "\"card_type\": \"ESSAY\"")))
        }
    }

    @Test
    fun `generation config body matches the TaskCreateRequest schema`() {
        val body = JSONObject(taskCreateBody(
            deckId = "d-1",
            chapterIds = listOf("c-1", "c-2"),
            config = com.qiuzhao.flashcards.domain.v25.V25GenerationConfig(
                coverageMode = V25CoverageMode.BALANCED,
                difficultyRatio = com.qiuzhao.flashcards.domain.v25.V25DifficultyRatio(40, 40, 20),
                customRequirements = "多举例子",
            ),
        ))
        assertEquals("d-1", body.getString("deck_id"))
        assertEquals(listOf("c-1", "c-2"), body.getJSONArray("chapter_ids").let { List(it.length()) { i -> it.getString(i) } })
        assertEquals("BALANCED", body.getJSONObject("generation_config").getString("coverage_mode"))
        assertEquals(40, body.getJSONObject("generation_config").getJSONObject("difficulty_ratio").getInt("basic"))
        assertEquals("多举例子", body.getJSONObject("generation_config").getString("custom_requirements"))
    }

    @Test
    fun `browse query maps the lowercase wire parameter values`() {
        val query = browseCardsQuery(com.qiuzhao.flashcards.domain.v25.V25BrowseFilter(
            order = V25BrowseOrder.random,
            contentDifficulty = V25ContentDifficulty.UNLABELED,
            mastery = V25MasteryFilter.mastered,
        ))
        assertEquals("?order=random&content_difficulty=UNLABELED&mastery=mastered", query)
    }

    @Test
    fun `rate card body carries the review event contract fields`() {
        val body = JSONObject(rateCardBody("c-1", com.qiuzhao.flashcards.domain.v25.V25Rating.GOOD))
        assertEquals("c-1", body.getString("card_id"))
        assertEquals("GOOD", body.getString("rating"))
        assertTrue(body.getString("client_event_id").isNotBlank())
    }

    // --- fixtures (verbatim OpenAPI shapes) ---

    private fun meBody(): JSONObject = JSONObject("""
        {"user": {"user_id": "u-1", "username": "alice", "email": "alice@example.com",
                  "avatar_key": "mood_03", "created_at": "2026-08-14T09:00:00Z"}}
    """.trimIndent())

    private fun preferencesBody(): String = """
        {"default_coverage_mode": "BALANCED",
         "default_difficulty_ratio": {"basic": 40, "understanding": 40, "deep_question": 20},
         "daily_learning_goal": 50, "learning_timezone": "Asia/Shanghai",
         "current_project_id": null, "updated_at": "2026-08-14T09:00:00Z"}
    """.trimIndent()

    private fun projectBody(): String = """
        {"project_id": "p-1", "name": "线性代数",
         "file": {"file_id": "f-1", "filename": "linear.pdf", "size_bytes": 1048576, "status": "PARSED",
                  "chapters": [{"chapter_id": "c-1", "name": "第一章 矩阵", "start_page": 1, "end_page": 20}]},
         "status": "READY", "chapter_count": 1, "deck_count": 2, "task_count": 3,
         "created_at": "2026-08-14T09:00:00Z", "updated_at": "2026-08-14T10:00:00Z",
         "version": "2026-08-14T10:00:00Z"}
    """.trimIndent()

    private fun taskBody(): String = """
        {"task_id": "t-1", "project_id": "p-1", "file_id": "f-1", "deck_id": "d-1", "retry_of_task_id": null,
         "status": "GENERATING", "internal_stage": "SCORING",
         "selected_chapters": [{"chapter_id": "c-1", "name": "第一章", "start_page": 1, "end_page": 20}],
         "generation_config": {"coverage_mode": "EXTENSIVE",
                               "difficulty_ratio": {"basic": 40, "understanding": 40, "deep_question": 20},
                               "custom_requirements": null},
         "sample_cards": null, "sample_config_hash": null, "sample_confirmed_at": null,
         "generated_card_count": 3, "skipped_planning_group_count": 0, "resumable": false,
         "error_code": null, "failure_stage": null,
         "created_at": "2026-08-14T09:00:00Z", "started_at": "2026-08-14T09:01:00Z", "ended_at": null,
         "updated_at": "2026-08-14T09:05:00Z"}
    """.trimIndent()

    private fun deckBody(): String = """
        {"deck_id": "d-1", "name": "线性代数", "source": "GENERATED", "project_id": "p-1",
         "card_count": 42, "due_count": 7, "mastered_card_count": 12, "review_count": 30,
         "mastery_ratio": 0.2857143,
         "created_at": "2026-08-14T09:00:00Z", "updated_at": "2026-08-14T10:00:00Z", "version": "v2"}
    """.trimIndent()

    private fun cardBody(): String = """
        {"card_id": "c-1", "deck_id": "d-1", "source": "GENERATED", "position": 3,
         "front": "什么是矩阵？", "back": "按行列排列的数表",
         "card_type": "QUESTION", "target_difficulty": "BASIC",
         "chapter_id": "ch-1", "source_task_id": "t-1",
         "publication_state": "PUBLISHED", "version": "v3",
         "created_at": "2026-08-14T09:00:00Z", "updated_at": "2026-08-14T10:00:00Z"}
    """.trimIndent()

    private fun validationErrorBody(): String =
        """{"error": {"code": "VALIDATION_ERROR", "message": "比例合计必须为 100", "localization_key": "error.validation"}}"""

    private fun expiredAuthBody(): String =
        """{"error": {"code": "AUTH_INVALID", "message": "会话已过期", "localization_key": "auth.invalid"}}"""

    private fun conflictBody(): String =
        """{"error": {"code": "CARD_VERSION_CONFLICT", "message": "卡片已被修改", "localization_key": "card.version.conflict"}}"""

    private fun deletionBatchBody(): String = """
        {"delete_batch_id": "b-1", "card_ids": ["c-1", "c-2"],
         "undo_until": "2026-08-15T09:00:10Z", "status": "PENDING",
         "created_at": "2026-08-15T09:00:00Z", "updated_at": "2026-08-15T09:00:00Z"}
    """.trimIndent()

    private fun rewritePreviewBody(): String = """
        {"rewrite_id": "r-1", "card_id": "c-1", "base_card_version": "v3",
         "front": "矩阵的秩是什么？", "back": "行向量组的最大无关组所含向量个数",
         "card_type": "QUESTION", "target_difficulty": "UNDERSTANDING",
         "custom_requirements": "强调直觉", "status": "PENDING",
         "expires_at": "2026-08-16T09:00:00Z", "created_at": "2026-08-15T09:00:00Z"}
    """.trimIndent()

    // period timestamps are real wire shapes: infra/db/session.py format_utc always emits
    // UTC ("...T16:00:00.000Z" = Asia/Shanghai Monday 00:00); the dashboard timezone field
    // names the actual bucketing zone.
    private fun statsBody(): String = """
        {"period": {"start": "2026-08-09T16:00:00.000Z", "end": "2026-08-15T15:59:59.000Z", "week_ordinal": 33},
         "timezone": "Asia/Shanghai",
         "weekly_activity": [5, 3, 0, 4, 2, 6, 1], "weekly_total": 21, "weekly_completed_count": 7,
         "week_change_rate": null, "weekly_goal": 350, "weekly_goal_progress": 0.02,
         "recall_accuracy": 0.8, "first_answer_accuracy": 0.75, "retention_rate": 0.82,
         "streak_days": 3, "mastered_card_count": 12,
         "updated_at": "2026-08-15T12:00:00Z", "has_data": true}
    """.trimIndent()

    private fun todayPlanEmptyBody(): String = """
        {"timezone": "Asia/Shanghai", "study_date": "2026-08-15",
         "current_project": null, "daily_goal": 50,
         "today_completed_count": 0, "due_count": 0, "main_plan_remaining": 0, "backlog_count": 0,
         "cards": []}
    """.trimIndent()

    private fun todayPlanBody(): String = """
        {"timezone": "Asia/Shanghai", "study_date": "2026-08-15",
         "current_project": {"project_id": "p-1", "name": "线性代数",
                             "file": {"file_id": "f-1", "filename": "linear.pdf", "size_bytes": 1048576, "status": "PARSED",
                                      "chapters": [{"chapter_id": "c-1", "name": "第一章", "start_page": 1, "end_page": 20}]},
                             "status": "READY", "chapter_count": 1, "deck_count": 2, "task_count": 3,
                             "created_at": "2026-08-14T09:00:00Z", "updated_at": "2026-08-14T10:00:00Z",
                             "version": "2026-08-14T10:00:00Z"},
         "daily_goal": 50, "today_completed_count": 3, "due_count": 2,
         "main_plan_remaining": 47, "backlog_count": 5,
         "cards": [{"card_id": "c-1", "deck_id": "d-1", "source": "GENERATED", "position": 1,
                    "front": "什么是矩阵？", "back": "数表", "card_type": "QUESTION",
                    "target_difficulty": "BASIC", "chapter_id": "ch-1", "source_task_id": "t-1",
                    "publication_state": "PUBLISHED", "version": "v1",
                    "created_at": "2026-08-14T09:00:00Z", "updated_at": "2026-08-14T09:00:00Z",
                    "review_state": {"review_state_id": "rs-1", "card_id": "c-1", "state": "NEW",
                                     "stability": 0.0, "difficulty": 1.0, "due": "2026-08-15T09:00:00Z",
                                     "reps": 0, "lapses": 0, "updated_at": "2026-08-14T09:00:00Z"},
                    "forgetting_risk": 0}]}
    """.trimIndent()

    private fun reviewQueueItemBody(): String = """
        {"card_id": "c-1", "deck_id": "d-1", "source": "GENERATED", "position": 1,
         "front": "什么是矩阵？", "back": "数表", "card_type": "QUESTION",
         "target_difficulty": "BASIC", "chapter_id": "ch-1", "source_task_id": "t-1",
         "publication_state": "PUBLISHED", "version": "v1",
         "created_at": "2026-08-14T09:00:00Z", "updated_at": "2026-08-14T09:00:00Z",
         "review_state": {"review_state_id": "rs-1", "card_id": "c-1", "state": "REVIEW",
                          "stability": 42.0, "difficulty": 3.0, "due": "2026-08-20T09:00:00Z",
                          "reps": 5, "lapses": 0, "updated_at": "2026-08-15T09:00:00Z"}}
    """.trimIndent()

    private fun ratingResultBody(): String = """
        {"review_state": {"review_state_id": "rs-1", "card_id": "c-1", "state": "LEARNING",
                          "stability": 1.0, "difficulty": 4.0, "due": "2026-08-15T09:10:00Z",
                          "reps": 1, "lapses": 0, "updated_at": "2026-08-15T09:05:00Z"},
         "study_date": "2026-08-15"}
    """.trimIndent()

    private fun apiKeyUnknownBody(): String =
        """{"status": "UNKNOWN", "masked_key": "", "updated_at": "2026-08-14T09:00:00Z"}"""

    private fun apiKeyAvailableBody(): String =
        """{"status": "AVAILABLE", "masked_key": "sk-****1234", "updated_at": "2026-08-14T09:00:00Z"}"""
}
