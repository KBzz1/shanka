package com.qiuzhao.flashcards.data.remote.v25

import android.content.Context
import com.qiuzhao.flashcards.data.remote.BackendClient
import com.qiuzhao.flashcards.data.remote.HttpResult
import com.qiuzhao.flashcards.data.session.KeystoreSessionStore
import com.qiuzhao.flashcards.data.session.SessionStore
import com.qiuzhao.flashcards.data.session.loadQuietly
import com.qiuzhao.flashcards.domain.v25.V25ApiKeyState
import com.qiuzhao.flashcards.domain.v25.V25ApiKeyStatus
import com.qiuzhao.flashcards.domain.v25.V25AuthUser
import com.qiuzhao.flashcards.domain.v25.V25AvatarKey
import com.qiuzhao.flashcards.domain.v25.V25BrowseFilter
import com.qiuzhao.flashcards.domain.v25.V25Card
import com.qiuzhao.flashcards.domain.v25.V25CardDeletionBatch
import com.qiuzhao.flashcards.domain.v25.V25CardDraft
import com.qiuzhao.flashcards.domain.v25.V25CardRewritePreview
import com.qiuzhao.flashcards.domain.v25.V25Chapter
import com.qiuzhao.flashcards.domain.v25.V25ChapterEdit
import com.qiuzhao.flashcards.domain.v25.V25Deck
import com.qiuzhao.flashcards.domain.v25.V25ErrorCodes
import com.qiuzhao.flashcards.domain.v25.V25GenerationConfig
import com.qiuzhao.flashcards.domain.v25.V25GenerationTask
import com.qiuzhao.flashcards.domain.v25.V25LearningProject
import com.qiuzhao.flashcards.domain.v25.V25PreferencesPatch
import com.qiuzhao.flashcards.domain.v25.V25ProjectStudySettings
import com.qiuzhao.flashcards.domain.v25.V25Rating
import com.qiuzhao.flashcards.domain.v25.V25RatingResult
import com.qiuzhao.flashcards.domain.v25.V25Repository
import com.qiuzhao.flashcards.domain.v25.V25Result
import com.qiuzhao.flashcards.domain.v25.V25ReviewCard
import com.qiuzhao.flashcards.domain.v25.V25SampleCard
import com.qiuzhao.flashcards.domain.v25.V25StatsDashboard
import com.qiuzhao.flashcards.domain.v25.V25StudySettingsPatch
import com.qiuzhao.flashcards.domain.v25.V25TaskConfigPatch
import com.qiuzhao.flashcards.domain.v25.V25TaskStatus
import com.qiuzhao.flashcards.domain.v25.V25TodayPlan
import com.qiuzhao.flashcards.domain.v25.V25UserPreferences
import java.io.InputStream
import org.json.JSONObject

private const val REDACTED = "[REDACTED]"

/**
 * The remote implementation of [V25Repository] (Architecture §8 `data/remote/v25`): every
 * method maps to exactly one endpoint of the committed OpenAPI contract, and every payload
 * or error becomes a typed [V25Result] — the visual lane never sees a DTO, HTTP status or
 * JSON object. It reuses the existing bearer-session and Idempotency-Key mechanisms through
 * [V25Transport] / [BackendClient] unchanged.
 *
 * Task 1 adjudication bindings honored here:
 * - [saveApiKey] receives the plaintext key (the upload flow requires it); the key travels
 *   only inside the PUT body over TLS, is never logged or persisted, and is redacted from
 *   any failure text before it crosses the boundary.
 * - [logout] has no token parameter: the implementation reads the stored session, clears it
 *   local-first, then revokes that explicit token server-side.
 * - `V25Deck.masteryRatio` is nullable; a missing wire ratio maps to null.
 *
 * Known mapping decisions (see V25Dtos.kt):
 * - The wire `version` is a string (`v\d+` or an ISO timestamp); the domain models it as an
 *   Int marker for cache-refresh change detection only.
 * - `V25StatsDashboard.progress` has no wire source yet (V25-STATS-FR-05 is not part of the
 *   StatsDashboard resource); it maps to the honest empty list.
 * - `V25PlanCard.isNew` is derived server-side semantics: `review_state.state == "NEW"`.
 */
class RemoteV25Repository(
    private val sessionStore: SessionStore,
    private val transport: V25Transport,
) : V25Repository {

    companion object {
        /** Production wiring: one shared session store for the client and the multipart transport. */
        fun create(context: Context): RemoteV25Repository {
            val sessionStore = KeystoreSessionStore(context)
            return RemoteV25Repository(
                sessionStore,
                BackendV25Transport(BackendClient(context, sessionStore = sessionStore), sessionStore),
            )
        }
    }

    // --- account profile (Architecture 4.1, V25-ACC) ------------------------------------------

    override suspend fun getAuthUser(): V25Result<V25AuthUser> =
        call("get_auth_user", "GET", "/auth/me", idempotent = false) { value ->
            parseAuthUser(requiredObject(value, "user"))
        }

    override suspend fun updateAuthUser(
        username: String?,
        avatarKey: V25AvatarKey?,
    ): V25Result<V25AuthUser> =
        call("update_auth_user", "PATCH", "/auth/me", authMeUpdateBody(username, avatarKey)) { value ->
            parseAuthUser(requiredObject(value, "user"))
        }

    /**
     * Local-first logout: the stored session is cleared before the server call, and the
     * explicit token is revoked so the request works even after the store is empty. With no
     * stored session the device is already signed out — an immediate success, no network.
     */
    override suspend fun logout(): V25Result<Unit> {
        val token = sessionStore.loadQuietly()?.token
        sessionStore.clear()
        if (token == null) return V25Result.Success(Unit)
        val result = transport.request("logout", "POST", "/auth/logout", token = token)
        return if (result.status in 200..299) V25Result.Success(Unit) else result.toFailure()
    }

    // --- preferences (Architecture 4.1, V25-SET) ----------------------------------------------

    override suspend fun getPreferences(): V25Result<V25UserPreferences> =
        call("get_preferences", "GET", "/preferences", idempotent = false, map = ::parseUserPreferences)

    override suspend fun updatePreferences(patch: V25PreferencesPatch): V25Result<V25UserPreferences> =
        call("update_preferences", "PATCH", "/preferences", preferencesPatchBody(patch), map = ::parseUserPreferences)

    override suspend fun setCurrentProject(projectId: String?): V25Result<V25UserPreferences> =
        call("set_current_project", "PATCH", "/preferences", setCurrentProjectBody(projectId), map = ::parseUserPreferences)

    // --- learning projects and chapters (Architecture 4.2) -------------------------------------

    override suspend fun createProject(
        fileName: String,
        content: InputStream,
        name: String?,
    ): V25Result<V25LearningProject> =
        upload("create_project", "/projects", fileName, content, name, map = ::parseLearningProject)

    override suspend fun listProjects(): V25Result<List<V25LearningProject>> =
        call("list_projects", "GET", "/projects", idempotent = false) { value ->
            requiredArray(value, "items").map(::parseLearningProject)
        }

    override suspend fun getProject(projectId: String): V25Result<V25LearningProject> =
        call("get_project", "GET", "/projects/$projectId", idempotent = false, map = ::parseLearningProject)

    override suspend fun renameProject(projectId: String, name: String): V25Result<V25LearningProject> =
        call("rename_project", "PATCH", "/projects/$projectId", renameBody(name), map = ::parseLearningProject)

    override suspend fun deleteProject(projectId: String, retainDecks: Boolean): V25Result<Unit> =
        call("delete_project", "DELETE", "/projects/$projectId?retain_decks=$retainDecks") { Unit }

    override suspend fun replaceProjectPdf(
        projectId: String,
        fileName: String,
        content: InputStream,
    ): V25Result<V25LearningProject> =
        upload("replace_project_pdf", "/projects/$projectId/replace-pdf", fileName, content, name = null, map = ::parseLearningProject)

    override suspend fun updateChapter(
        projectId: String,
        chapterId: String,
        edit: V25ChapterEdit,
    ): V25Result<V25Chapter> =
        call("update_chapter", "PATCH", "/projects/$projectId/chapters/$chapterId", chapterEditBody(edit), map = ::parseChapter)

    override suspend fun deleteChapter(
        projectId: String,
        chapterId: String,
        deleteCards: Boolean,
    ): V25Result<Unit> =
        call("delete_chapter", "DELETE", "/projects/$projectId/chapters/$chapterId" + if (deleteCards) "?delete_cards=true" else "") { Unit }

    override suspend fun confirmChapters(projectId: String): V25Result<V25LearningProject> =
        call("confirm_chapters", "POST", "/projects/$projectId/confirm-chapters", map = ::parseLearningProject)

    override suspend fun getStudySettings(projectId: String): V25Result<V25ProjectStudySettings> =
        call("get_study_settings", "GET", "/projects/$projectId/study-settings", idempotent = false) { value ->
            parseStudySettings(projectId, value)
        }

    override suspend fun updateStudySettings(
        projectId: String,
        patch: V25StudySettingsPatch,
    ): V25Result<V25ProjectStudySettings> =
        call("update_study_settings", "PATCH", "/projects/$projectId/study-settings", studySettingsPatchBody(patch)) { value ->
            parseStudySettings(projectId, value)
        }

    // --- generation tasks (Architecture 4.3) ---------------------------------------------------

    override suspend fun createTask(
        projectId: String,
        deckId: String,
        chapterIds: List<String>,
        config: V25GenerationConfig,
    ): V25Result<V25GenerationTask> =
        call("create_task", "POST", "/projects/$projectId/tasks", taskCreateBody(deckId, chapterIds, config), map = ::parseGenerationTask)

    override suspend fun listTasks(
        projectId: String?,
        status: V25TaskStatus?,
    ): V25Result<List<V25GenerationTask>> =
        call("list_tasks", "GET", queryPath("/tasks", "project_id" to projectId, "status" to status?.name), idempotent = false) { value ->
            requiredArray(value, "items").map(::parseGenerationTask)
        }

    override suspend fun getTask(taskId: String): V25Result<V25GenerationTask> =
        call("get_task", "GET", "/tasks/$taskId", idempotent = false, map = ::parseGenerationTask)

    override suspend fun updateTaskConfig(
        taskId: String,
        patch: V25TaskConfigPatch,
    ): V25Result<V25GenerationTask> =
        call("update_task", "PATCH", "/tasks/$taskId", taskConfigPatchBody(patch), map = ::parseGenerationTask)

    /** The samples endpoint returns the updated task; its persisted sample cards are the payload. */
    override suspend fun generateSamples(taskId: String): V25Result<List<V25SampleCard>> =
        call("generate_samples", "POST", "/tasks/$taskId/samples", map = ::parseGenerationTask)
            .mapSuccess { it.sampleCards }

    override suspend fun startTask(taskId: String): V25Result<V25GenerationTask> =
        call("start_task", "POST", "/tasks/$taskId/start", map = ::parseGenerationTask)

    override suspend fun abandonTask(taskId: String): V25Result<V25GenerationTask> =
        call("abandon_task", "POST", "/tasks/$taskId/abandon", map = ::parseGenerationTask)

    override suspend fun retryTask(taskId: String): V25Result<V25GenerationTask> =
        call("retry_task", "POST", "/tasks/$taskId/retry", map = ::parseGenerationTask)

    override suspend fun deleteTask(taskId: String, deleteGeneratedCards: Boolean): V25Result<Unit> =
        call("delete_task", "DELETE", "/tasks/$taskId" + if (deleteGeneratedCards) "?delete_generated_cards=true" else "") { Unit }

    // --- decks and cards (Architecture 4.4) -----------------------------------------------------

    override suspend fun listDecks(projectId: String?): V25Result<List<V25Deck>> =
        call("list_decks", "GET", queryPath("/decks", "project_id" to projectId), idempotent = false) { value ->
            requiredArray(value, "items").map(::parseDeck)
        }

    override suspend fun createDeck(name: String, projectId: String?): V25Result<V25Deck> =
        call("create_deck", "POST", "/decks", createDeckBody(name, projectId), map = ::parseDeck)

    override suspend fun getDeck(deckId: String): V25Result<V25Deck> =
        call("get_deck", "GET", "/decks/$deckId", idempotent = false, map = ::parseDeck)

    override suspend fun renameDeck(deckId: String, name: String): V25Result<V25Deck> =
        call("rename_deck", "PATCH", "/decks/$deckId", renameBody(name), map = ::parseDeck)

    override suspend fun deleteDeck(deckId: String): V25Result<Unit> =
        call("delete_deck", "DELETE", "/decks/$deckId") { Unit }

    override suspend fun listCards(deckId: String, filter: V25BrowseFilter): V25Result<List<V25Card>> =
        call("list_cards", "GET", "/decks/$deckId/cards" + browseCardsQuery(filter), idempotent = false) { value ->
            requiredArray(value, "items").map(::parseCard)
        }

    /**
     * One POST per draft to the single-card endpoint: the interface promises the created
     * cards come back, and only that endpoint returns full [V25Card] payloads (the bulk
     * import endpoint returns per-index ids only). Stops at the first failure; each request
     * carries its own Idempotency-Key, so a retried call never duplicates a card.
     */
    override suspend fun addCards(deckId: String, drafts: List<V25CardDraft>): V25Result<List<V25Card>> {
        val created = mutableListOf<V25Card>()
        for (draft in drafts) {
            when (val result = call("create_card", "POST", "/decks/$deckId/cards", cardDraftBody(draft.front, draft.back), map = ::parseCard)) {
                is V25Result.Success -> created += result.value
                is V25Result.Failure -> return result
            }
        }
        return V25Result.Success(created)
    }

    override suspend fun updateCard(cardId: String, front: String, back: String): V25Result<V25Card> =
        call("update_card", "PATCH", "/cards/$cardId", cardDraftBody(front, back), map = ::parseCard)

    /**
     * The delete_batch_id of the last successful deletion is remembered and appended to the
     * next delete, so consecutive deletions merge into one pending batch and re-arm its
     * 10-second window (openapi deleteCard semantics). A failed delete keeps the id, so the
     * next successful attempt still joins the same still-pending batch.
     */
    override suspend fun deleteCard(cardId: String): V25Result<V25CardDeletionBatch> {
        val path = lastDeleteBatchId?.let { "/cards/$cardId?delete_batch_id=$it" } ?: "/cards/$cardId"
        return call("delete_card", "DELETE", path, map = ::parseDeletionBatch)
            .also { if (it is V25Result.Success) lastDeleteBatchId = it.value.deleteBatchId }
    }

    // --- deletion undo (Architecture 4.4 / 3.7) --------------------------------------------------

    override suspend fun pendingDeletionBatches(): V25Result<List<V25CardDeletionBatch>> =
        call("pending_deletion_batches", "GET", "/card-deletion-batches/pending", idempotent = false) { value ->
            requiredArray(value, "items").map(::parseDeletionBatch)
        }

    override suspend fun undoDeletionBatch(deleteBatchId: String): V25Result<Unit> =
        call("undo_deletion_batch", "POST", "/card-deletion-batches/$deleteBatchId/undo") { Unit }

    // --- AI rewrite previews (Architecture 4.4 / 3.8) --------------------------------------------

    override suspend fun createRewritePreview(
        cardId: String,
        customRequirements: String?,
    ): V25Result<V25CardRewritePreview> =
        call("create_rewrite_preview", "POST", "/cards/$cardId/rewrite-previews", rewritePreviewBody(customRequirements), map = ::parseRewritePreview)

    override suspend fun applyRewritePreview(cardId: String, rewriteId: String): V25Result<V25Card> =
        call("apply_rewrite_preview", "POST", "/cards/$cardId/rewrite-previews/$rewriteId/apply", map = ::parseCard)

    override suspend fun cancelRewritePreview(cardId: String, rewriteId: String): V25Result<Unit> =
        call("cancel_rewrite_preview", "DELETE", "/cards/$cardId/rewrite-previews/$rewriteId") { Unit }

    // --- study and review (Architecture 4.5) ------------------------------------------------------

    override suspend fun todayPlan(): V25Result<V25TodayPlan> =
        call("today_plan", "GET", "/study/today", idempotent = false, map = ::parseTodayPlan)

    override suspend fun deckReviewQueue(deckId: String): V25Result<List<V25ReviewCard>> =
        call("review_queue", "GET", "/decks/$deckId/review", idempotent = false) { value ->
            requiredArray(value, "items").map(::parseReviewCard)
        }

    override suspend fun rateCard(cardId: String, rating: V25Rating): V25Result<V25RatingResult> =
        call("submit_review", "POST", "/review-events", rateCardBody(cardId, rating), map = ::parseRatingResult)

    // --- statistics (Architecture 4.5) ------------------------------------------------------------

    override suspend fun statsDashboard(): V25Result<V25StatsDashboard> =
        call("dashboard", "GET", "/stats/dashboard", idempotent = false, map = ::parseStatsDashboard)

    // --- AI service key (V25-SET-FR-05) -------------------------------------------------------------

    override suspend fun apiKeyStatus(): V25Result<V25ApiKeyStatus> =
        call("api_key_status", "GET", "/api-key/status", idempotent = false, map = ::parseApiKeyStatus)

    /**
     * The plaintext key travels only inside the PUT /api-key request body over TLS. This
     * implementation never logs, persists or echoes it: failure text is redacted below if
     * the server ever reflected it, and the key never reaches a debug log (the v25 layer
     * writes no log lines at all). Upstream verification being unavailable (502
     * API_KEY_UNAVAILABLE) is the PRD's "暂时无法验证" status, so it maps to a Success with
     * [V25ApiKeyState.VERIFICATION_UNAVAILABLE]; a failed validation keeps the old key
     * server-side (structure-contract 6.2).
     */
    override suspend fun saveApiKey(apiKey: String): V25Result<V25ApiKeyStatus> {
        val result = transport.request("save_api_key", "PUT", "/api-key", apiKeyBody(apiKey))
        if (result.status == 502 && parseError(result.body)?.code == "API_KEY_UNAVAILABLE") {
            return V25Result.Success(V25ApiKeyStatus(V25ApiKeyState.VERIFICATION_UNAVAILABLE, null))
        }
        if (result.status !in 200..299) return result.toFailure().withoutKey(apiKey)
        return runCatching { V25Result.Success(parseApiKeyStatus(jsonObject(result.body))) }
            .getOrElse { V25Result.Failure(V25ErrorCodes.INVALID_RESPONSE, null, it.message) }
    }

    // --- plumbing -----------------------------------------------------------------------------------

    /** Tracks the last pending deletion batch so consecutive deletes merge (see [deleteCard]). */
    private var lastDeleteBatchId: String? = null

    private suspend fun <T> call(
        operation: String,
        method: String,
        path: String,
        body: String? = null,
        idempotent: Boolean = true,
        token: String? = null,
        map: (JSONObject) -> T,
    ): V25Result<T> {
        val result = transport.request(operation, method, path, body, idempotent = idempotent, token = token)
        if (result.status !in 200..299) return result.toFailure()
        return runCatching { V25Result.Success(map(jsonObject(result.body))) }
            .getOrElse { V25Result.Failure(V25ErrorCodes.INVALID_RESPONSE, null, it.message) }
    }

    private suspend fun <T> upload(
        operation: String,
        path: String,
        fileName: String,
        content: InputStream,
        name: String?,
        map: (JSONObject) -> T,
    ): V25Result<T> {
        val result = transport.upload(operation, path, fileName, content, name)
        if (result.status !in 200..299) return result.toFailure()
        return runCatching { V25Result.Success(map(jsonObject(result.body))) }
            .getOrElse { V25Result.Failure(V25ErrorCodes.INVALID_RESPONSE, null, it.message) }
    }

    private fun HttpResult.toFailure(): V25Result.Failure {
        val envelope = parseError(body)
        return V25Result.Failure(
            code = envelope?.code ?: "HTTP_$status",
            localizationKey = envelope?.localizationKey,
            message = envelope?.message,
        )
    }

    /** Defense in depth for [saveApiKey]: never let the plaintext cross the boundary in text. */
    private fun V25Result.Failure.withoutKey(key: String): V25Result.Failure =
        V25Result.Failure(
            code = code,
            localizationKey = localizationKey?.replace(key, REDACTED),
            message = message?.replace(key, REDACTED),
        )

    private fun <T, R> V25Result<T>.mapSuccess(transform: (T) -> R): V25Result<R> = when (this) {
        is V25Result.Success -> V25Result.Success(transform(value))
        is V25Result.Failure -> this
    }

    private fun queryPath(base: String, vararg pairs: Pair<String, String?>): String = buildString {
        append(base)
        var first = true
        for ((key, value) in pairs) {
            if (value == null) continue
            append(if (first) "?" else "&")
            first = false
            append(key).append("=").append(value)
        }
    }
}
