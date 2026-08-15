package com.qiuzhao.flashcards.data.remote.v25

import com.qiuzhao.flashcards.domain.v25.V25ApiKeyState
import com.qiuzhao.flashcards.domain.v25.V25ApiKeyStatus
import com.qiuzhao.flashcards.domain.v25.V25AuthUser
import com.qiuzhao.flashcards.domain.v25.V25AvatarKey
import com.qiuzhao.flashcards.domain.v25.V25BrowseFilter
import com.qiuzhao.flashcards.domain.v25.V25Card
import com.qiuzhao.flashcards.domain.v25.V25CardDeletionBatch
import com.qiuzhao.flashcards.domain.v25.V25CardRewritePreview
import com.qiuzhao.flashcards.domain.v25.V25CardType
import com.qiuzhao.flashcards.domain.v25.V25Chapter
import com.qiuzhao.flashcards.domain.v25.V25ChapterEdit
import com.qiuzhao.flashcards.domain.v25.V25CoverageMode
import com.qiuzhao.flashcards.domain.v25.V25CurrentProject
import com.qiuzhao.flashcards.domain.v25.V25DailyActivity
import com.qiuzhao.flashcards.domain.v25.V25Deck
import com.qiuzhao.flashcards.domain.v25.V25DeletionBatchStatus
import com.qiuzhao.flashcards.domain.v25.V25Difficulty
import com.qiuzhao.flashcards.domain.v25.V25DifficultyRatio
import com.qiuzhao.flashcards.domain.v25.V25GenerationConfig
import com.qiuzhao.flashcards.domain.v25.V25GenerationTask
import com.qiuzhao.flashcards.domain.v25.V25InternalStage
import com.qiuzhao.flashcards.domain.v25.V25LearningProject
import com.qiuzhao.flashcards.domain.v25.V25PdfFile
import com.qiuzhao.flashcards.domain.v25.V25PlanCard
import com.qiuzhao.flashcards.domain.v25.V25PreferencesPatch
import com.qiuzhao.flashcards.domain.v25.V25ProjectStatus
import com.qiuzhao.flashcards.domain.v25.V25ProjectStudySettings
import com.qiuzhao.flashcards.domain.v25.V25PublicationState
import com.qiuzhao.flashcards.domain.v25.V25Rating
import com.qiuzhao.flashcards.domain.v25.V25RatingResult
import com.qiuzhao.flashcards.domain.v25.V25ReviewCard
import com.qiuzhao.flashcards.domain.v25.V25ReviewState
import com.qiuzhao.flashcards.domain.v25.V25RewriteStatus
import com.qiuzhao.flashcards.domain.v25.V25SampleCard
import com.qiuzhao.flashcards.domain.v25.V25StatsDashboard
import com.qiuzhao.flashcards.domain.v25.V25StudySettingsPatch
import com.qiuzhao.flashcards.domain.v25.V25TaskConfigPatch
import com.qiuzhao.flashcards.domain.v25.V25TaskStatus
import com.qiuzhao.flashcards.domain.v25.V25TodayPlan
import com.qiuzhao.flashcards.domain.v25.V25UserPreferences
import java.time.Instant
import java.time.LocalDate
import java.time.OffsetDateTime
import java.util.UUID
import org.json.JSONArray
import org.json.JSONObject
import org.json.JSONTokener

/**
 * Wire layer of the V2.5 remote data tier (Architecture §8 `data/remote/v25`). Every function
 * here maps one OpenAPI payload — or builds one request body — against the committed backend
 * contract (`openapi.yaml` + `structure-contract.md` section 3). Nothing in this file is visible
 * to the visual lane: it consumes only [com.qiuzhao.flashcards.domain.v25.V25Repository] and the
 * typed models.
 *
 * Contract rules enforced here:
 * - Required fields (per the OpenAPI `required` sets) are strict: missing, blank or mistyped
 *   values throw and surface as `INVALID_RESPONSE` in the repository.
 * - Optional/nullable fields map to null; the wire `""` (e.g. an unset `masked_key`) is null.
 * - Enum values are the exact wire strings; unknown values throw (contract violation).
 * - Timestamps are ISO-8601; `period.start` may carry a non-UTC offset (account learning
 *   timezone), so week-day derivation goes through [OffsetDateTime].
 * - `version` on the wire is a string (`v\d+` from the rewrite CAS or an ISO timestamp from
 *   `version=now`); the boundary models it as an Int marker that only serves cache-refresh
 *   change detection — `updated_at` / `base_card_version` stay authoritative.
 */

// --- JSON helpers --------------------------------------------------------------------------

internal fun jsonObject(body: String): JSONObject {
    if (body.isBlank()) return JSONObject()
    return JSONTokener(body).nextValue() as? JSONObject
        ?: throw IllegalArgumentException("response is not a JSON object")
}

internal fun requiredArray(value: JSONObject, key: String): List<JSONObject> {
    val array = value.optJSONArray(key) ?: throw IllegalArgumentException("missing array '$key'")
    return List(array.length()) { index -> array.getJSONObject(index) }
}

internal fun optionalArray(value: JSONObject, key: String): List<JSONObject> {
    val array = value.optJSONArray(key) ?: return emptyList()
    return List(array.length()) { index -> array.getJSONObject(index) }
}

internal fun requiredString(value: JSONObject, key: String): String =
    value.optString(key).takeIf { it.isNotBlank() }
        ?: throw IllegalArgumentException("missing or blank '$key'")

internal fun optionalString(value: JSONObject, key: String): String? =
    value.optString(key).takeIf { it.isNotBlank() }

internal fun requiredInt(value: JSONObject, key: String): Int {
    if (!value.has(key) || value.isNull(key)) throw IllegalArgumentException("missing int '$key'")
    return value.getInt(key)
}

internal fun optionalInt(value: JSONObject, key: String): Int? =
    if (value.has(key) && !value.isNull(key)) value.getInt(key) else null

internal fun requiredInstant(value: JSONObject, key: String): Instant =
    parseIsoInstant(requiredString(value, key), key)

internal fun optionalInstant(value: JSONObject, key: String): Instant? =
    optionalString(value, key)?.let { parseIsoInstant(it, key) }

/** The backend serializes UTC datetimes with a `+00:00` offset; tolerate `Z` and sub-second precision. */
internal fun parseIsoInstant(wire: String, key: String): Instant =
    runCatching { Instant.parse(wire) }
        .getOrElse { runCatching { OffsetDateTime.parse(wire).toInstant() }.getOrElse { throw IllegalArgumentException("invalid $key timestamp '$wire'") } }

internal fun requiredDate(value: JSONObject, key: String): LocalDate =
    LocalDate.parse(requiredString(value, key))

internal inline fun <reified T : Enum<T>> requiredEnum(value: JSONObject, key: String): T =
    parseEnum(requiredString(value, key), key)

internal inline fun <reified T : Enum<T>> optionalEnum(value: JSONObject, key: String): T? =
    optionalString(value, key)?.let { java.lang.Enum.valueOf(T::class.java, it) }

/** Enum entry names are the exact wire values (V25Models.kt contract); unknown values throw. */
internal inline fun <reified T : Enum<T>> parseEnum(wire: String, key: String): T =
    java.lang.Enum.valueOf(T::class.java, wire)

/** Wire `version` is a string: `v\d+` (rewrite CAS) or an ISO timestamp (`version=now`). */
internal fun parseVersion(wire: String?): Int = when {
    wire == null -> 0
    wire.startsWith("v") -> wire.drop(1).toIntOrNull() ?: 0
    else -> runCatching { Instant.parse(wire).epochSecond.toInt() }.getOrDefault(0)
}

// --- payload mappers (wire JSON → domain models) ----------------------------------------------

internal fun parseAuthUser(value: JSONObject): V25AuthUser = V25AuthUser(
    userId = requiredString(value, "user_id"),
    username = requiredString(value, "username"),
    email = requiredString(value, "email"),
    avatarKey = requiredEnum<V25AvatarKey>(value, "avatar_key"),
    createdAt = requiredInstant(value, "created_at"),
)

internal fun parseDifficultyRatio(value: JSONObject): V25DifficultyRatio = V25DifficultyRatio(
    basic = requiredInt(value, "basic"),
    understanding = requiredInt(value, "understanding"),
    deepQuestion = requiredInt(value, "deep_question"),
)

internal fun parseGenerationConfig(value: JSONObject): V25GenerationConfig = V25GenerationConfig(
    coverageMode = requiredEnum<V25CoverageMode>(value, "coverage_mode"),
    difficultyRatio = parseDifficultyRatio(requiredObject(value, "difficulty_ratio")),
    customRequirements = optionalString(value, "custom_requirements").orEmpty(),
)

internal fun parseUserPreferences(value: JSONObject): V25UserPreferences = V25UserPreferences(
    defaultCoverageMode = requiredEnum<V25CoverageMode>(value, "default_coverage_mode"),
    difficultyRatio = parseDifficultyRatio(requiredObject(value, "default_difficulty_ratio")),
    dailyLearningGoal = requiredInt(value, "daily_learning_goal"),
    learningTimezone = requiredString(value, "learning_timezone"),
    currentProjectId = optionalString(value, "current_project_id"),
    updatedAt = requiredInstant(value, "updated_at"),
)

internal fun parseChapter(value: JSONObject): V25Chapter = V25Chapter(
    id = requiredString(value, "chapter_id"),
    name = requiredString(value, "name"),
    startPage = requiredInt(value, "start_page"),
    endPage = requiredInt(value, "end_page"),
)

internal fun parsePdfFile(value: JSONObject): V25PdfFile = V25PdfFile(
    id = requiredString(value, "file_id"),
    name = requiredString(value, "filename"),
    chapters = optionalArray(value, "chapters").map(::parseChapter),
)

internal fun parseLearningProject(value: JSONObject): V25LearningProject = V25LearningProject(
    projectId = requiredString(value, "project_id"),
    name = requiredString(value, "name"),
    file = parsePdfFile(requiredObject(value, "file")),
    status = requiredEnum<V25ProjectStatus>(value, "status"),
    chapterCount = requiredInt(value, "chapter_count"),
    deckCount = requiredInt(value, "deck_count"),
    taskCount = requiredInt(value, "task_count"),
    createdAt = requiredInstant(value, "created_at"),
    updatedAt = requiredInstant(value, "updated_at"),
    version = parseVersion(optionalString(value, "version")),
)

internal fun parseStudySettings(projectId: String, value: JSONObject): V25ProjectStudySettings =
    V25ProjectStudySettings(
        projectId = projectId,
        selectedNewCardChapterIds = stringList(value, "selected_new_card_chapter_ids"),
        includeUnassigned = requiredBoolean(value, "include_unassigned"),
        updatedAt = requiredInstant(value, "updated_at"),
    )

internal fun parseSampleCard(value: JSONObject): V25SampleCard = V25SampleCard(
    front = requiredString(value, "front"),
    back = requiredString(value, "back"),
    cardType = requiredEnum<V25CardType>(value, "card_type"),
    // Persisted samples are one per enabled tier, so the target difficulty is always present.
    targetDifficulty = requiredEnum<V25Difficulty>(value, "target_difficulty"),
)

internal fun parseGenerationTask(value: JSONObject): V25GenerationTask = V25GenerationTask(
    taskId = requiredString(value, "task_id"),
    projectId = optionalString(value, "project_id"),
    fileId = optionalString(value, "file_id"),
    deckId = optionalString(value, "deck_id"),
    retryOfTaskId = optionalString(value, "retry_of_task_id"),
    status = requiredEnum<V25TaskStatus>(value, "status"),
    internalStage = optionalEnum<V25InternalStage>(value, "internal_stage"),
    selectedChapters = requiredArray(value, "selected_chapters").map(::parseChapter),
    generationConfig = parseGenerationConfig(requiredObject(value, "generation_config")),
    sampleCards = optionalArray(value, "sample_cards").map(::parseSampleCard),
    sampleConfigHash = optionalString(value, "sample_config_hash"),
    sampleConfirmedAt = optionalInstant(value, "sample_confirmed_at"),
    generatedCardCount = requiredInt(value, "generated_card_count"),
    errorCode = optionalString(value, "error_code"),
    failureStage = optionalString(value, "failure_stage"),
    createdAt = requiredInstant(value, "created_at"),
    startedAt = optionalInstant(value, "started_at"),
    endedAt = optionalInstant(value, "ended_at"),
    updatedAt = requiredInstant(value, "updated_at"),
)

internal fun parseDeck(value: JSONObject): V25Deck = V25Deck(
    deckId = requiredString(value, "deck_id"),
    name = requiredString(value, "name"),
    projectId = optionalString(value, "project_id"),
    cardCount = requiredInt(value, "card_count"),
    dueCount = requiredInt(value, "due_count"),
    masteredCards = requiredInt(value, "mastered_card_count"),
    reviewCount = requiredInt(value, "review_count"),
    masteryRatio = optionalFloat(value, "mastery_ratio"),
)

internal fun parseCard(value: JSONObject): V25Card = V25Card(
    cardId = requiredString(value, "card_id"),
    deckId = requiredString(value, "deck_id"),
    front = requiredString(value, "front"),
    back = requiredString(value, "back"),
    cardType = requiredEnum<V25CardType>(value, "card_type"),
    targetDifficulty = optionalEnum<V25Difficulty>(value, "target_difficulty"),
    position = requiredInt(value, "position"),
    chapterId = optionalString(value, "chapter_id"),
    sourceTaskId = optionalString(value, "source_task_id"),
    publicationState = optionalEnum<V25PublicationState>(value, "publication_state") ?: V25PublicationState.PUBLISHED,
    version = parseVersion(optionalString(value, "version")),
)

internal fun parseReviewState(value: JSONObject): V25ReviewState = V25ReviewState(
    state = requiredString(value, "state"),
    due = optionalInstant(value, "due"),
)

/** ReviewQueueItem / TodayPlanCard: the card fields are flattened next to review_state. */
internal fun parseReviewCard(value: JSONObject): V25ReviewCard = V25ReviewCard(
    card = parseCard(value),
    reviewState = value.optJSONObject("review_state")?.let(::parseReviewState),
)

internal fun parseDeletionBatch(value: JSONObject): V25CardDeletionBatch = V25CardDeletionBatch(
    deleteBatchId = requiredString(value, "delete_batch_id"),
    cardIds = stringList(value, "card_ids"),
    undoUntil = requiredInstant(value, "undo_until"),
    status = requiredEnum<V25DeletionBatchStatus>(value, "status"),
    createdAt = requiredInstant(value, "created_at"),
    updatedAt = requiredInstant(value, "updated_at"),
)

internal fun parseRewritePreview(value: JSONObject): V25CardRewritePreview = V25CardRewritePreview(
    rewriteId = requiredString(value, "rewrite_id"),
    cardId = requiredString(value, "card_id"),
    baseCardVersion = requiredString(value, "base_card_version"),
    front = requiredString(value, "front"),
    back = requiredString(value, "back"),
    cardType = requiredEnum<V25CardType>(value, "card_type"),
    targetDifficulty = optionalEnum<V25Difficulty>(value, "target_difficulty"),
    customRequirements = optionalString(value, "custom_requirements"),
    status = requiredEnum<V25RewriteStatus>(value, "status"),
    expiresAt = requiredInstant(value, "expires_at"),
)

internal fun parsePlanCard(value: JSONObject): V25PlanCard = V25PlanCard(
    card = parseCard(value),
    isNew = value.optJSONObject("review_state")?.optString("state") == "NEW",
    reviewState = value.optJSONObject("review_state")?.let(::parseReviewState),
)

internal fun parseTodayPlan(value: JSONObject): V25TodayPlan = V25TodayPlan(
    learningTimezone = requiredString(value, "timezone"),
    studyDate = requiredDate(value, "study_date"),
    currentProject = value.optJSONObject("current_project")?.let { project ->
        V25CurrentProject(
            projectId = requiredString(project, "project_id"),
            name = requiredString(project, "name"),
        )
    },
    dailyGoal = requiredInt(value, "daily_goal"),
    completedCount = requiredInt(value, "today_completed_count"),
    dueCount = requiredInt(value, "due_count"),
    planRemaining = requiredInt(value, "main_plan_remaining"),
    backlogCount = requiredInt(value, "backlog_count"),
    cards = requiredArray(value, "cards").map(::parsePlanCard),
)

internal fun parseRatingResult(value: JSONObject): V25RatingResult = V25RatingResult(
    reviewState = parseReviewState(requiredObject(value, "review_state")),
    studyDate = requiredDate(value, "study_date"),
)

internal fun parseStatsDashboard(value: JSONObject): V25StatsDashboard {
    val period = requiredObject(value, "period")
    // period.start is Monday midnight in the account learning timezone — parse through the
    // offset so the local date is the account-local Monday, not the UTC projection.
    val weekStart = OffsetDateTime.parse(requiredString(period, "start")).toLocalDate()
    val activityArray = value.optJSONArray("weekly_activity")
        ?: throw IllegalArgumentException("missing array 'weekly_activity'")
    val activity = List(activityArray.length()) { index ->
        V25DailyActivity(studyDate = weekStart.plusDays(index.toLong()), ratingCount = activityArray.getInt(index))
    }
    return V25StatsDashboard(
        hasData = requiredBoolean(value, "has_data"),
        weeklyActivity = activity,
        weeklyTotalRatings = requiredInt(value, "weekly_total"),
        weeklyChangeRate = optionalFloat(value, "week_change_rate"),
        weeklyGoal = requiredInt(value, "weekly_goal"),
        weeklyGoalCompleted = requiredInt(value, "weekly_completed_count"),
        weeklyGoalRate = optionalFloat(value, "weekly_goal_progress"),
        recallAccuracy = optionalFloat(value, "recall_accuracy"),
        firstAttemptAccuracy = optionalFloat(value, "first_answer_accuracy"),
        retentionRate = optionalFloat(value, "retention_rate"),
        streakDays = requiredInt(value, "streak_days"),
        masteredCards = requiredInt(value, "mastered_card_count"),
        // The dashboard wire carries no per-scope progress summaries (V25-STATS-FR-05 is not
        // part of the StatsDashboard resource yet); keep the honest empty list instead of
        // fabricating numbers. Project/deck progress is derivable from decks + cards instead.
        progress = emptyList(),
        updatedAt = requiredInstant(value, "updated_at"),
    )
}

internal fun apiKeyState(wire: String): V25ApiKeyState = when (wire) {
    "AVAILABLE" -> V25ApiKeyState.AVAILABLE
    "INVALID" -> V25ApiKeyState.INVALID
    "INSUFFICIENT_BALANCE" -> V25ApiKeyState.INSUFFICIENT_BALANCE
    // structure-contract 3.1: UNKNOWN = 未保存 Key（masked_key 为空串）→ 域模型 UNSET。
    "UNKNOWN" -> V25ApiKeyState.UNSET
    else -> throw IllegalArgumentException("unknown api key status '$wire'")
}

internal fun parseApiKeyStatus(value: JSONObject): V25ApiKeyStatus = V25ApiKeyStatus(
    state = apiKeyState(requiredString(value, "status")),
    maskedKey = optionalString(value, "masked_key"),
)

// --- error envelope (structure-contract 1.4) ---------------------------------------------------

internal data class V25ErrorEnvelope(
    val code: String?,
    val localizationKey: String?,
    val message: String?,
)

internal fun parseError(body: String): V25ErrorEnvelope? = runCatching {
    val error = jsonObject(body).optJSONObject("error") ?: return null
    V25ErrorEnvelope(
        code = error.optString("code").takeIf { it.isNotBlank() },
        localizationKey = error.optString("localization_key").takeIf { it.isNotBlank() },
        message = error.optString("message").takeIf { it.isNotBlank() },
    )
}.getOrNull()

// --- request bodies -----------------------------------------------------------------------------

internal fun authMeUpdateBody(username: String?, avatarKey: V25AvatarKey?): String = JSONObject()
    .apply {
        if (username != null) put("username", username)
        if (avatarKey != null) put("avatar_key", avatarKey.name)
    }
    .toString()

internal fun preferencesPatchBody(patch: V25PreferencesPatch): String = JSONObject()
    .apply {
        patch.defaultCoverageMode?.let { put("default_coverage_mode", it.name) }
        patch.difficultyRatio?.let { put("default_difficulty_ratio", difficultyRatioObject(it)) }
        patch.dailyLearningGoal?.let { put("daily_learning_goal", it) }
        patch.learningTimezone?.let { put("learning_timezone", it) }
    }
    .toString()

/** The wire field is `[string, 'null']`; a cleared project must serialize as an explicit null. */
internal fun setCurrentProjectBody(projectId: String?): String =
    JSONObject().put("current_project_id", projectId ?: JSONObject.NULL).toString()

internal fun chapterEditBody(edit: V25ChapterEdit): String = JSONObject()
    .put("name", edit.name)
    .put("start_page", edit.startPage)
    .put("end_page", edit.endPage)
    .toString()

internal fun studySettingsPatchBody(patch: V25StudySettingsPatch): String = JSONObject()
    .apply {
        patch.selectedNewCardChapterIds?.let { put("selected_new_card_chapter_ids", JSONArray(it)) }
        patch.includeUnassigned?.let { put("include_unassigned", it) }
    }
    .toString()

internal fun difficultyRatioObject(ratio: V25DifficultyRatio): JSONObject = JSONObject()
    .put("basic", ratio.basic)
    .put("understanding", ratio.understanding)
    .put("deep_question", ratio.deepQuestion)

internal fun generationConfigObject(config: V25GenerationConfig): JSONObject = JSONObject()
    .put("coverage_mode", config.coverageMode.name)
    .put("difficulty_ratio", difficultyRatioObject(config.difficultyRatio))
    .apply { if (config.customRequirements.isNotBlank()) put("custom_requirements", config.customRequirements) }

internal fun taskCreateBody(deckId: String, chapterIds: List<String>, config: V25GenerationConfig): String =
    JSONObject()
        .put("deck_id", deckId)
        .put("chapter_ids", JSONArray(chapterIds))
        .put("generation_config", generationConfigObject(config))
        .toString()

internal fun taskConfigPatchBody(patch: V25TaskConfigPatch): String = JSONObject()
    .apply {
        patch.deckId?.let { put("deck_id", it) }
        patch.chapterIds?.let { put("chapter_ids", JSONArray(it)) }
        patch.generationConfig?.let { put("generation_config", generationConfigObject(it)) }
    }
    .toString()

internal fun createDeckBody(name: String, projectId: String?): String = JSONObject()
    .put("name", name)
    .apply { if (projectId != null) put("project_id", projectId) }
    .toString()

internal fun renameBody(name: String): String = JSONObject().put("name", name).toString()

internal fun cardDraftBody(front: String, back: String): String =
    JSONObject().put("front", front).put("back", back).toString()

/** ReviewEventRequest: client_event_id is the device-unique offline-retry idempotency key. */
internal fun rateCardBody(cardId: String, rating: V25Rating): String = JSONObject()
    .put("card_id", cardId)
    .put("rating", rating.name)
    .put("client_event_id", UUID.randomUUID().toString())
    .toString()

internal fun apiKeyBody(apiKey: String): String = JSONObject().put("api_key", apiKey).toString()

/** The wire field is `[string, 'null']`; no requirements still serialize as an explicit null. */
internal fun rewritePreviewBody(customRequirements: String?): String = JSONObject()
    .put("custom_requirements", customRequirements ?: JSONObject.NULL)
    .toString()

/** Free-browse query (Architecture 4.4): lowercase wire values, optional content-difficulty filter. */
internal fun browseCardsQuery(filter: V25BrowseFilter): String = buildString {
    append("?order=").append(filter.order.name)
    filter.contentDifficulty?.let { append("&content_difficulty=").append(it.name) }
    append("&mastery=").append(filter.mastery.name)
}

// --- misc helpers -------------------------------------------------------------------------------

/** A primitive-string array (e.g. `selected_new_card_chapter_ids`, `card_ids`). */
internal fun stringList(value: JSONObject, key: String): List<String> {
    val array = value.optJSONArray(key) ?: throw IllegalArgumentException("missing array '$key'")
    return List(array.length()) { index -> array.getString(index) }
}

internal fun requiredObject(value: JSONObject, key: String): JSONObject =
    value.optJSONObject(key) ?: throw IllegalArgumentException("missing object '$key'")

internal fun requiredBoolean(value: JSONObject, key: String): Boolean {
    if (!value.has(key) || value.isNull(key)) throw IllegalArgumentException("missing boolean '$key'")
    return value.getBoolean(key)
}

internal fun optionalFloat(value: JSONObject, key: String): Float? {
    if (!value.has(key) || value.isNull(key)) return null
    return value.getDouble(key).toFloat()
}
