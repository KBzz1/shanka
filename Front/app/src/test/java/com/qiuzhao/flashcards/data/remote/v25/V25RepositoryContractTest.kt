package com.qiuzhao.flashcards.data.remote.v25

import com.qiuzhao.flashcards.data.remote.HttpResult
import com.qiuzhao.flashcards.data.session.Session
import com.qiuzhao.flashcards.data.session.SessionStore
import com.qiuzhao.flashcards.data.session.SessionUser
import com.qiuzhao.flashcards.domain.v25.V25ApiKeyState
import com.qiuzhao.flashcards.domain.v25.V25BrowseFilter
import com.qiuzhao.flashcards.domain.v25.V25BrowseOrder
import com.qiuzhao.flashcards.domain.v25.V25CardDraft
import com.qiuzhao.flashcards.domain.v25.V25ContentDifficulty
import com.qiuzhao.flashcards.domain.v25.V25CoverageMode
import com.qiuzhao.flashcards.domain.v25.V25DifficultyRatio
import com.qiuzhao.flashcards.domain.v25.V25ErrorCodes
import com.qiuzhao.flashcards.domain.v25.V25GenerationConfig
import com.qiuzhao.flashcards.domain.v25.V25ImportStatus
import com.qiuzhao.flashcards.domain.v25.V25MasteryFilter
import com.qiuzhao.flashcards.domain.v25.V25PreferencesPatch
import com.qiuzhao.flashcards.domain.v25.V25Rating
import com.qiuzhao.flashcards.domain.v25.V25Repository
import com.qiuzhao.flashcards.domain.v25.V25Result
import com.qiuzhao.flashcards.domain.v25.isAuthFailure
import java.io.ByteArrayInputStream
import java.util.concurrent.CancellationException
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Locks the repository's transport behavior on the JVM: which requests carry auth and
 * idempotency semantics, how failures and cancellation surface, and that the plaintext
 * API key never leaves the request body. The fake transport records every call; the fake
 * session store lets the logout flow run without Android.
 */
class V25RepositoryContractTest {

    private val user = SessionUser(userId = "u-1", username = "alice", createdAt = "2026-08-14T00:00:00Z")
    private val session = Session(token = "token-1", user = user)

    private class FakeSessionStore(initial: Session? = null) : SessionStore {
        var session: Session? = initial
        val savedTokens = mutableListOf<String>()
        var clearCount = 0
        override fun save(token: String, user: SessionUser) {
            session = Session(token, user)
            savedTokens += token
        }

        override fun load(): Session? = session
        override fun clear() {
            session = null
            clearCount++
        }
    }

    private class RecordedCall(
        val operation: String,
        val method: String,
        val path: String,
        val body: String?,
        val idempotent: Boolean,
        val authenticate: Boolean,
        val token: String?,
        val idempotencyKey: String? = null,
        val fileName: String? = null,
        val name: String? = null,
    )

    private class FakeTransport : V25Transport {
        val calls = mutableListOf<RecordedCall>()
        var handler: (RecordedCall) -> HttpResult = { HttpResult(200, "{}", emptyMap()) }

        override suspend fun request(
            operation: String,
            method: String,
            path: String,
            body: String?,
            contentType: String,
            idempotent: Boolean,
            authenticate: Boolean,
            token: String?,
            idempotencyKey: String?,
        ): HttpResult {
            val call = RecordedCall(operation, method, path, body, idempotent, authenticate, token, idempotencyKey = idempotencyKey)
            calls += call
            return handler(call)
        }

        override suspend fun upload(
            operation: String,
            path: String,
            fileName: String,
            content: java.io.InputStream,
            name: String?,
            idempotencyKey: String?,
        ): HttpResult {
            val call = RecordedCall(operation, "POST", path, null, idempotent = true, authenticate = true, token = null, idempotencyKey = idempotencyKey, fileName = fileName, name = name)
            calls += call
            return handler(call)
        }
    }

    private fun repository(store: FakeSessionStore, transport: FakeTransport): V25Repository =
        RemoteV25Repository(store, transport)

    // --- auth headers and idempotency keys ------------------------------------------------

    @Test
    fun `reads authenticate but never carry an idempotency key`() = runBlocking {
        val store = FakeSessionStore(session)
        val transport = FakeTransport()
        val repo = repository(store, transport)

        repo.getAuthUser()
        repo.listProjects()
        repo.statsDashboard()

        assertEquals(3, transport.calls.size)
        transport.calls.forEach {
            assertEquals("reads must authenticate with the stored session", true, it.authenticate)
            assertEquals("reads must not request an Idempotency-Key", false, it.idempotent)
        }
        assertEquals(listOf("GET", "GET", "GET"), transport.calls.map { it.method })
        assertEquals(listOf("/auth/me", "/projects", "/stats/dashboard"), transport.calls.map { it.path })
    }

    @Test
    fun `mutations carry the idempotency flag for the client to attach a key`() = runBlocking {
        val transport = FakeTransport()
        val repo = repository(FakeSessionStore(session), transport)

        repo.updatePreferences(V25PreferencesPatch(dailyLearningGoal = 60))
        repo.rateCard("c-1", V25Rating.GOOD)
        repo.deleteCard("c-1")

        assertEquals(3, transport.calls.size)
        transport.calls.forEach { assertEquals(true, it.idempotent) }
    }

    @Test
    fun `default writes pass no caller key so the client keeps generating fresh ones`() = runBlocking {
        val transport = FakeTransport()
        val repo = repository(FakeSessionStore(session), transport)

        repo.createDeck("线性代数")
        repo.updatePreferences(V25PreferencesPatch(dailyLearningGoal = 60))

        assertEquals(2, transport.calls.size)
        transport.calls.forEach {
            assertEquals(true, it.idempotent)
            assertNull("no caller key: the client auto-generates one per request", it.idempotencyKey)
        }
    }

    @Test
    fun `query parameters follow the contract paths`() = runBlocking {
        val transport = FakeTransport()
        val repo = repository(FakeSessionStore(session), transport)

        repo.listTasks(projectId = "p-1", status = com.qiuzhao.flashcards.domain.v25.V25TaskStatus.DRAFT)
        repo.listDecks(projectId = "p-1")
        repo.listCards(
            "d-1",
            V25BrowseFilter(
                order = V25BrowseOrder.random,
                contentDifficulty = V25ContentDifficulty.UNLABELED,
                mastery = V25MasteryFilter.unmastered,
            ),
        )
        repo.deleteProject("p-1", retainDecks = false)
        repo.deleteChapter("p-1", "c-1", deleteCards = true)
        repo.deleteTask("t-1", deleteGeneratedCards = true)

        assertEquals("/tasks?project_id=p-1&status=DRAFT", transport.calls[0].path)
        assertEquals("/decks?project_id=p-1", transport.calls[1].path)
        assertEquals("/decks/d-1/cards?order=random&content_difficulty=UNLABELED&mastery=unmastered", transport.calls[2].path)
        assertEquals("/projects/p-1?retain_decks=false", transport.calls[3].path)
        assertEquals("/projects/p-1/chapters/c-1?delete_cards=true", transport.calls[4].path)
        assertEquals("/tasks/t-1?delete_generated_cards=true", transport.calls[5].path)
    }

    @Test
    fun `optional false query flags are omitted from the path`() = runBlocking {
        val transport = FakeTransport()
        val repo = repository(FakeSessionStore(session), transport)

        repo.deleteChapter("p-1", "c-1", deleteCards = false)
        repo.deleteTask("t-1", deleteGeneratedCards = false)

        assertEquals("/projects/p-1/chapters/c-1", transport.calls[0].path)
        assertEquals("/tasks/t-1", transport.calls[1].path)
    }

    @Test
    fun `deletion operations carry the explicit decision and stable retry key`() = runBlocking {
        val transport = FakeTransport()
        val repo = repository(FakeSessionStore(session), transport)

        repo.deleteProject(
            "p-1",
            retainDecks = false,
            abandonPreGenerationTasks = true,
            idempotencyKey = "project-key",
        )
        repo.getProjectDeletionPreflight("p-1", retainDecks = false)
        repo.deleteDeck("d-1", abandonPreGenerationTasks = true, idempotencyKey = "deck-key")
        repo.getDeckDeletionPreflight("d-1")

        assertEquals("/projects/p-1?retain_decks=false&abandon_pre_generation_tasks=true", transport.calls[0].path)
        assertEquals("project-key", transport.calls[0].idempotencyKey)
        assertEquals("/projects/p-1/deletion-preflight?retain_decks=false", transport.calls[1].path)
        assertFalse(transport.calls[1].idempotent)
        assertEquals("/decks/d-1?abandon_pre_generation_tasks=true", transport.calls[2].path)
        assertEquals("deck-key", transport.calls[2].idempotencyKey)
        assertEquals("/decks/d-1/deletion-preflight", transport.calls[3].path)
        assertFalse(transport.calls[3].idempotent)
    }

    @Test
    fun `cancellation decision is part of deletion path and preflight`() = runBlocking {
        val transport = FakeTransport()
        val repo = repository(FakeSessionStore(session), transport)

        repo.deleteProject(
            "p-1",
            retainDecks = false,
            cancelActiveTasks = true,
            idempotencyKey = "project-cancel-key",
        )
        repo.getProjectDeletionPreflight("p-1", retainDecks = false, allowCancel = true)
        repo.deleteDeck(
            "d-1",
            cancelActiveTasks = true,
            idempotencyKey = "deck-cancel-key",
        )
        repo.getDeckDeletionPreflight("d-1", allowCancel = true)

        assertEquals(
            "/projects/p-1?retain_decks=false&cancel_active_tasks=true",
            transport.calls[0].path,
        )
        assertEquals("project-cancel-key", transport.calls[0].idempotencyKey)
        assertEquals(
            "/projects/p-1/deletion-preflight?retain_decks=false&cancel_active_tasks=true",
            transport.calls[1].path,
        )
        assertEquals(
            "/decks/d-1?cancel_active_tasks=true",
            transport.calls[2].path,
        )
        assertEquals("deck-cancel-key", transport.calls[2].idempotencyKey)
        assertEquals(
            "/decks/d-1/deletion-preflight?cancel_active_tasks=true",
            transport.calls[3].path,
        )
    }

    // --- logout ----------------------------------------------------------------------------

    @Test
    fun `logout revokes the stored token and clears the store first`() = runBlocking {
        val store = FakeSessionStore(session)
        val transport = FakeTransport()
        val repo = repository(store, transport)

        val result = repo.logout()

        assertTrue(result is V25Result.Success)
        assertEquals(1, store.clearCount)
        val call = transport.calls.single()
        assertEquals("POST", call.method)
        assertEquals("/auth/logout", call.path)
        assertEquals("token-1", call.token)
        assertEquals(true, call.idempotent)
    }

    @Test
    fun `logout without a session is an immediate local success`() = runBlocking {
        val store = FakeSessionStore(null)
        val transport = FakeTransport()
        val repo = repository(store, transport)

        val result = repo.logout()

        assertTrue(result is V25Result.Success)
        assertTrue(transport.calls.isEmpty())
        assertEquals(1, store.clearCount)
    }

    @Test
    fun `logout keeps the local clear even when revocation fails`() = runBlocking {
        val store = FakeSessionStore(session)
        val transport = FakeTransport().apply {
            handler = { HttpResult(401, authErrorBody("AUTH_INVALID"), emptyMap()) }
        }
        val repo = repository(store, transport)

        val result = repo.logout()

        assertTrue(result is V25Result.Failure)
        assertTrue((result as V25Result.Failure).isAuthFailure)
        assertEquals(1, store.clearCount)
    }

    // --- deletion batch chaining ------------------------------------------------------------

    @Test
    fun `deleteCard appends the pending batch id and adopts the newest batch`() = runBlocking {
        val transport = FakeTransport().apply {
            handler = { call ->
                when (call.path.substringBefore("?")) {
                    "/cards/c-1" -> HttpResult(200, deletionBatchBody("b-1"), emptyMap())
                    "/cards/c-2" -> HttpResult(200, deletionBatchBody("b-2"), emptyMap())
                    "/cards/c-3" -> HttpResult(200, deletionBatchBody("b-2"), emptyMap())
                    else -> error("unexpected path ${call.path}")
                }
            }
        }
        val repo = repository(FakeSessionStore(session), transport)

        repo.deleteCard("c-1")
        repo.deleteCard("c-2")
        repo.deleteCard("c-3")

        assertEquals("/cards/c-1", transport.calls[0].path)
        assertEquals("/cards/c-2?delete_batch_id=b-1", transport.calls[1].path)
        assertEquals("/cards/c-3?delete_batch_id=b-2", transport.calls[2].path)
    }

    @Test
    fun `a failed delete keeps the batch id for the next attempt`() = runBlocking {
        val transport = FakeTransport().apply {
            handler = { call ->
                if (call.path == "/cards/c-1") HttpResult(200, deletionBatchBody("b-1"), emptyMap())
                else HttpResult(503, networkErrorBody(), emptyMap())
            }
        }
        val repo = repository(FakeSessionStore(session), transport)

        repo.deleteCard("c-1")
        val failure = repo.deleteCard("c-2")
        repo.deleteCard("c-3")

        assertTrue(failure is V25Result.Failure)
        assertEquals(V25ErrorCodes.NETWORK_UNAVAILABLE, (failure as V25Result.Failure).code)
        assertEquals("/cards/c-3?delete_batch_id=b-1", transport.calls[2].path)
    }

    // --- recoverable failures and cancellation ----------------------------------------------

    @Test
    fun `transport failures map to coded results instead of throwing`() = runBlocking {
        val transport = FakeTransport().apply {
            handler = {
                when (it.path) {
                    "/preferences" -> HttpResult(429, rateLimitBody(), emptyMap())
                    "/decks" -> HttpResult(503, networkErrorBody(), emptyMap())
                    else -> HttpResult(500, """{"error": {"code": "INTERNAL_ERROR", "message": "boom"}}""", emptyMap())
                }
            }
        }
        val repo = repository(FakeSessionStore(session), transport)

        val limited = repo.getPreferences()
        val offline = repo.listDecks()
        val internal = repo.getProject("p-1")

        assertEquals("RATE_LIMITED", (limited as V25Result.Failure).code)
        assertEquals("rate.limited", limited.localizationKey)
        assertEquals(V25ErrorCodes.NETWORK_UNAVAILABLE, (offline as V25Result.Failure).code)
        assertEquals("INTERNAL_ERROR", (internal as V25Result.Failure).code)
    }

    @Test
    fun `auth failures surface as coded failures that read as auth`() = runBlocking {
        val transport = FakeTransport().apply {
            handler = { HttpResult(401, authErrorBody("AUTH_REQUIRED"), emptyMap()) }
        }
        val repo = repository(FakeSessionStore(session), transport)

        val result = repo.getAuthUser() as V25Result.Failure

        assertEquals("AUTH_REQUIRED", result.code)
        assertTrue(result.isAuthFailure)
    }

    @Test
    fun `cancellation propagates instead of becoming a failure`() {
        val transport = FakeTransport().apply {
            handler = { throw CancellationException("job cancelled") }
        }
        val repo = repository(FakeSessionStore(session), transport)

        assertThrows(CancellationException::class.java) { runBlocking { repo.getDeck("d-1") } }
    }

    @Test
    fun `a response without an error envelope gets a stable HTTP fallback code`() = runBlocking {
        val transport = FakeTransport().apply {
            handler = { HttpResult(502, """{"detail": "bad gateway"}""", emptyMap()) }
        }
        val repo = repository(FakeSessionStore(session), transport)

        val result = repo.getTask("t-1") as V25Result.Failure

        assertEquals("HTTP_502", result.code)
    }

    @Test
    fun `malformed success payloads become INVALID_RESPONSE failures`() = runBlocking {
        val transport = FakeTransport().apply {
            handler = { HttpResult(200, """{"unexpected": true}""", emptyMap()) }
        }
        val repo = repository(FakeSessionStore(session), transport)

        val result = repo.getDeck("d-1") as V25Result.Failure

        assertEquals(V25ErrorCodes.INVALID_RESPONSE, result.code)
    }

    // --- API key safety -----------------------------------------------------------------------

    @Test
    fun `saveApiKey sends the plaintext only in the request body and never stores it`() = runBlocking {
        val store = FakeSessionStore(session)
        val transport = FakeTransport().apply {
            handler = { HttpResult(200, apiKeyAvailableBody(), emptyMap()) }
        }
        val repo = repository(store, transport)

        val result = repo.saveApiKey("sk-secret-1234")

        assertTrue(result is V25Result.Success)
        assertEquals("sk-****1234", (result as V25Result.Success).value.maskedKey)
        val body = transport.calls.single().body!!
        assertEquals("sk-secret-1234", org.json.JSONObject(body).getString("api_key"))
        assertTrue("the repository must never persist anything, let alone the key", store.savedTokens.isEmpty())
    }

    @Test
    fun `saveApiKey maps upstream unavailability to the verification status`() = runBlocking {
        val transport = FakeTransport().apply {
            handler = { HttpResult(502, """{"error": {"code": "API_KEY_UNAVAILABLE", "message": "上游不可用"}}""", emptyMap()) }
        }
        val repo = repository(FakeSessionStore(session), transport)

        val result = repo.saveApiKey("sk-secret-1234")

        assertTrue(result is V25Result.Success)
        assertEquals(
            V25ApiKeyState.VERIFICATION_UNAVAILABLE,
            (result as V25Result.Success).value.state,
        )
        assertNull(result.value.maskedKey)
    }

    @Test
    fun `saveApiKey failures never echo the plaintext key`() = runBlocking {
        val transport = FakeTransport().apply {
            handler = { HttpResult(401, authErrorBody("AUTH_INVALID", message = "rejected sk-secret-1234"), emptyMap()) }
        }
        val repo = repository(FakeSessionStore(session), transport)

        val result = repo.saveApiKey("sk-secret-1234") as V25Result.Failure

        assertFalse(result.message.orEmpty().contains("sk-secret-1234"))
        assertFalse(result.localizationKey.orEmpty().contains("sk-secret-1234"))
    }

    @Test
    fun `saveApiKey redacts the plaintext key from every failure field including code`() = runBlocking {
        // Worst-case server reflection: the key echoed back in every envelope field.
        val transport = FakeTransport().apply {
            handler = {
                HttpResult(
                    401,
                    """{"error": {"code": "sk-secret-1234", "message": "rejected sk-secret-1234", "localization_key": "sk-secret-1234"}}""",
                    emptyMap(),
                )
            }
        }
        val repo = repository(FakeSessionStore(session), transport)

        val result = repo.saveApiKey("sk-secret-1234") as V25Result.Failure

        assertFalse(result.code.contains("sk-secret-1234"))
        assertFalse(result.message.orEmpty().contains("sk-secret-1234"))
        assertFalse(result.localizationKey.orEmpty().contains("sk-secret-1234"))
    }

    @Test
    fun `apiKeyStatus maps the wire UNKNOWN state to UNSET`() = runBlocking {
        val transport = FakeTransport().apply {
            handler = { HttpResult(200, """{"status": "UNKNOWN", "masked_key": "", "updated_at": "2026-08-14T09:00:00Z"}""", emptyMap()) }
        }
        val repo = repository(FakeSessionStore(session), transport)

        val result = repo.apiKeyStatus()

        assertTrue(result is V25Result.Success)
        assertEquals(V25ApiKeyState.UNSET, (result as V25Result.Success).value.state)
        assertNull(result.value.maskedKey)
    }

    // --- uploads and card adds ---------------------------------------------------------------

    @Test
    fun `createProject uploads the pdf with the file name and optional project name`() = runBlocking {
        val transport = FakeTransport().apply {
            handler = { HttpResult(201, projectBody(), emptyMap()) }
        }
        val repo = repository(FakeSessionStore(session), transport)

        val result = repo.createProject("linear.pdf", ByteArrayInputStream(byteArrayOf(1, 2)), name = "线性代数")

        assertTrue(result is V25Result.Success)
        assertEquals("p-1", (result as V25Result.Success).value.projectId)
        val call = transport.calls.single()
        assertEquals("/projects", call.path)
        assertEquals("linear.pdf", call.fileName)
        assertEquals("线性代数", call.name)
    }

    @Test
    fun `replaceProjectPdf uploads to the replace endpoint without a name`() = runBlocking {
        val transport = FakeTransport().apply {
            handler = { HttpResult(200, projectBody(), emptyMap()) }
        }
        val repo = repository(FakeSessionStore(session), transport)

        repo.replaceProjectPdf("p-1", "linear-v2.pdf", ByteArrayInputStream(byteArrayOf(1, 2)))

        val call = transport.calls.single()
        assertEquals("/projects/p-1/replace-pdf", call.path)
        assertEquals("linear-v2.pdf", call.fileName)
        assertNull(call.name)
    }

    @Test
    fun `importCards sends one atomic bulk request and maps the per-index results`() = runBlocking {
        val transport = FakeTransport().apply {
            handler = { call ->
                if (call.path == "/decks/d-1/cards/import") HttpResult(201, importResponseBody(), emptyMap())
                else error("unexpected path ${call.path}")
            }
        }
        val repo = repository(FakeSessionStore(session), transport)

        val result = repo.importCards(
            "d-1",
            listOf(V25CardDraft("什么是矩阵？", "数表"), V25CardDraft("什么是秩？", "最大无关组")),
        )

        assertTrue(result is V25Result.Success)
        val results = (result as V25Result.Success).value
        assertEquals(1, transport.calls.size)
        assertEquals("/decks/d-1/cards/import", transport.calls.single().path)
        assertEquals(true, transport.calls.single().idempotent)
        assertEquals(2, results.size)
        assertEquals(0, results[0].index)
        assertEquals(V25ImportStatus.CREATED, results[0].status)
        assertEquals("c-1", results[0].cardId)
        // 一次原子请求：请求体携带全部草稿，而不是逐张 POST。
        val cards = org.json.JSONObject(transport.calls.single().body!!).getJSONArray("cards")
        assertEquals(2, cards.length())
        assertEquals("什么是矩阵？", cards.getJSONObject(0).getString("front"))
    }

    @Test
    fun `importCards carries the caller idempotency key for a retried batch`() = runBlocking {
        val transport = FakeTransport().apply {
            handler = { HttpResult(201, importResponseBody(), emptyMap()) }
        }
        val repo = repository(FakeSessionStore(session), transport)

        repo.importCards("d-1", listOf(V25CardDraft("正面", "背面")), idempotencyKey = "batch-key-1")

        assertEquals("batch-key-1", transport.calls.single().idempotencyKey)
    }

    @Test
    fun `importCards maps a FAILED row without throwing`() = runBlocking {
        val transport = FakeTransport().apply {
            handler = {
                HttpResult(
                    201,
                    """{"results":[{"index":0,"status":"FAILED","error":{"field":"front"}}]}""",
                    emptyMap(),
                )
            }
        }
        val repo = repository(FakeSessionStore(session), transport)

        val result = repo.importCards("d-1", listOf(V25CardDraft("", "")))

        assertTrue(result is V25Result.Success)
        val row = (result as V25Result.Success).value.single()
        assertEquals(V25ImportStatus.FAILED, row.status)
        assertNull(row.cardId)
    }

    @Test
    fun `generateSamples returns the persisted sample cards from the task payload`() = runBlocking {
        val transport = FakeTransport().apply {
            handler = { HttpResult(200, taskWithSamplesBody(), emptyMap()) }
        }
        val repo = repository(FakeSessionStore(session), transport)

        val result = repo.generateSamples("t-1")

        assertTrue(result is V25Result.Success)
        assertEquals(1, (result as V25Result.Success).value.size)
        assertEquals("什么是矩阵？", result.value[0].front)
        assertEquals("/tasks/t-1/samples", transport.calls.single().path)
    }

    // --- request bodies -----------------------------------------------------------------------

    @Test
    fun `updateAuthUser sends only the provided fields`() = runBlocking {
        val transport = FakeTransport()
        val repo = repository(FakeSessionStore(session), transport)

        repo.updateAuthUser(username = "bob")

        val body = org.json.JSONObject(transport.calls.single().body!!)
        assertEquals("bob", body.getString("username"))
        assertFalse(body.has("avatar_key"))
    }

    @Test
    fun `setCurrentProject sends an explicit null to clear`() = runBlocking {
        val transport = FakeTransport()
        val repo = repository(FakeSessionStore(session), transport)

        repo.setCurrentProject(null)

        val body = org.json.JSONObject(transport.calls.single().body!!)
        assertTrue(body.has("current_project_id"))
        assertTrue(body.isNull("current_project_id"))
    }

    @Test
    fun `createTask posts to the project tasks endpoint`() = runBlocking {
        val transport = FakeTransport().apply {
            handler = { HttpResult(201, taskBody(), emptyMap()) }
        }
        val repo = repository(FakeSessionStore(session), transport)

        val result = repo.createTask(
            projectId = "p-1",
            deckId = "d-1",
            chapterIds = listOf("c-1"),
            config = V25GenerationConfig(V25CoverageMode.BALANCED, V25DifficultyRatio(40, 40, 20)),
        )

        assertTrue(result is V25Result.Success)
        assertEquals("/projects/p-1/tasks", transport.calls.single().path)
        assertEquals("d-1", org.json.JSONObject(transport.calls.single().body!!).getString("deck_id"))
    }

    // --- fixture bodies ------------------------------------------------------------------------

    private fun authErrorBody(code: String, message: String = "认证失败"): String =
        """{"error": {"code": "$code", "message": "$message", "localization_key": "auth.invalid"}}"""

    private fun rateLimitBody(): String =
        """{"error": {"code": "RATE_LIMITED", "message": "请求过于频繁", "localization_key": "rate.limited"}}"""

    private fun networkErrorBody(): String =
        """{"error": {"code": "NETWORK_UNAVAILABLE", "message": "网络不可用"}}"""

    private fun deletionBatchBody(batchId: String): String = """
        {"delete_batch_id": "$batchId", "card_ids": ["c-1"],
         "undo_until": "2026-08-15T09:00:10Z", "status": "PENDING",
         "created_at": "2026-08-15T09:00:00Z", "updated_at": "2026-08-15T09:00:00Z"}
    """.trimIndent()

    private fun apiKeyAvailableBody(): String =
        """{"status": "AVAILABLE", "masked_key": "sk-****1234", "updated_at": "2026-08-14T09:00:00Z"}"""

    private fun projectBody(): String = """
        {"project_id": "p-1", "name": "线性代数",
         "file": {"file_id": "f-1", "filename": "linear.pdf", "size_bytes": 1, "status": "PARSED",
                  "chapters": [{"chapter_id": "c-1", "name": "第一章", "start_page": 1, "end_page": 20}]},
         "status": "READY", "chapter_count": 1, "deck_count": 0, "task_count": 0,
         "created_at": "2026-08-14T09:00:00Z", "updated_at": "2026-08-14T10:00:00Z",
         "version": "2026-08-14T10:00:00Z"}
    """.trimIndent()

    private fun importResponseBody(): String = """
        {"results":[
          {"index":0,"status":"CREATED","card_id":"c-1"},
          {"index":1,"status":"CREATED","card_id":"c-2"}
        ]}
    """.trimIndent()

    private fun taskBody(): String = """
        {"task_id": "t-1", "project_id": "p-1", "file_id": "f-1", "deck_id": "d-1", "retry_of_task_id": null,
         "status": "DRAFT", "internal_stage": null,
         "selected_chapters": [{"chapter_id": "c-1", "name": "第一章", "start_page": 1, "end_page": 20}],
         "generation_config": {"coverage_mode": "BALANCED",
                               "difficulty_ratio": {"basic": 40, "understanding": 40, "deep_question": 20},
                               "custom_requirements": null},
         "sample_cards": null, "sample_config_hash": null, "sample_confirmed_at": null,
         "generated_card_count": 0, "skipped_planning_group_count": 0, "resumable": false,
         "error_code": null, "failure_stage": null,
         "created_at": "2026-08-14T09:00:00Z", "started_at": null, "ended_at": null,
         "updated_at": "2026-08-14T09:00:00Z"}
    """.trimIndent()

    private fun taskWithSamplesBody(): String = taskBody().replace(
        "\"sample_cards\": null",
        """ "sample_cards": [{"card_id": "s-1", "front": "什么是矩阵？", "back": "数表", "card_type": "QUESTION", "target_difficulty": "BASIC"}] """,
    )
}
