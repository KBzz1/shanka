package com.qiuzhao.flashcards.data.offline

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import com.qiuzhao.flashcards.data.local.ShankaV25Database
import com.qiuzhao.flashcards.data.local.V25CacheStore
import com.qiuzhao.flashcards.data.remote.http.NetworkStack
import com.qiuzhao.flashcards.data.remote.v25.RemoteV25Repository
import com.qiuzhao.flashcards.data.session.InMemorySessionStore
import com.qiuzhao.flashcards.data.session.SessionUser
import com.qiuzhao.flashcards.domain.v25.V25ProjectStatus
import com.qiuzhao.flashcards.domain.v25.V25Result
import java.io.File
import java.io.IOException
import java.time.Clock
import java.time.Instant
import java.time.ZoneId
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.Dispatcher
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * Parse-wait discriminating suite (Robolectric + MockWebServer + file-backed `shanka-v25.db`,
 * mirroring [OfflineFoundationTest]): the five-minute project cache must never mask a finished
 * server-side parse when the caller forces the network, and a forced read must fall back to
 * the cached snapshot offline instead of pretending the data is gone.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class OfflineParseWaitTest {

    class TestClock(val nowMs: Long = Instant.parse("2026-08-31T00:00:00Z").toEpochMilli()) : Clock() {
        override fun getZone(): ZoneId = ZoneId.of("UTC")
        override fun withZone(zone: ZoneId): Clock = this
        override fun instant(): Instant = Instant.ofEpochMilli(nowMs)
    }

    /** Fake backend: one project whose parse status the test flips between requests. */
    class ProjectsBackend : Dispatcher() {
        @Volatile var offline = false

        /** Status served by GET /projects (list) and GET /projects/{id} (detail) independently. */
        @Volatile var listParsed = false
        @Volatile var detailParsed = false
        var listRequestCount = 0
        var detailRequestCount = 0

        override fun dispatch(request: RecordedRequest): MockResponse {
            if (offline) throw IOException("airplane mode")
            val path = request.path ?: return notFound()
            return when {
                path == "/projects" -> {
                    listRequestCount++
                    ok("""{"items": [${projectBody(parsed = listParsed)}]}""")
                }
                path.startsWith("/projects/") -> {
                    detailRequestCount++
                    ok(projectBody(parsed = detailParsed))
                }
                else -> notFound()
            }
        }

        private fun ok(body: String) = MockResponse().setResponseCode(200).setBody(body)
        private fun notFound() = MockResponse().setResponseCode(404).setBody("""{"error":{"code":"NOT_FOUND"}}""")
    }

    private lateinit var server: MockWebServer
    private lateinit var backend: ProjectsBackend
    private lateinit var store: InMemorySessionStore
    private lateinit var dbFile: File

    private val user1 = SessionUser(userId = "u-1", username = "alice", createdAt = "2026-08-01T00:00:00Z")

    @Before
    fun setUp() {
        server = MockWebServer()
        backend = ProjectsBackend()
        server.dispatcher = backend
        server.start()
        store = InMemorySessionStore()
        store.save("token-u1", user1)
        val context = ApplicationProvider.getApplicationContext<Context>()
        dbFile = File(context.cacheDir, "offline-parse-wait-${System.nanoTime()}.db")
    }

    @After
    fun tearDown() {
        server.shutdown()
    }

    /** Builds the same object graph the app assembles, against the fake backend. */
    private fun buildStack(db: ShankaV25Database, repoScope: CoroutineScope): OfflineFirstV25Repository {
        val stack = NetworkStack(store, baseUrlOverride = server.url("/").toString())
        val remote = RemoteV25Repository.create(stack)
        val cache = V25CacheStore(db)
        val lanes = RequestLanes(repoScope)
        val sync = ReviewSyncCoordinator(
            remote = remote,
            cache = cache,
            sessionUser = { store.load()?.user?.userId },
            clock = TestClock(),
            lanes = lanes,
            scope = repoScope,
            onAuthoritativeRefreshNeeded = {},
        )
        return OfflineFirstV25Repository(
            remote = remote,
            cache = cache,
            sessionStore = store,
            lanes = lanes,
            reviewSync = sync,
            clock = TestClock(),
        )
    }

    private fun openDatabase(): ShankaV25Database {
        val context = ApplicationProvider.getApplicationContext<Context>()
        return ShankaV25Database.buildOnFile(context, dbFile.absolutePath)
    }

    private fun scope() = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    // --- 1. forced read bypasses a fresh cache and rewrites the projection ---------------------------

    @Test
    fun test_offline_getProject_forceRefresh_bypasses_fresh_cache_and_rewrites_projection() = runBlocking {
        val db = openDatabase()
        val repo = buildStack(db, scope())
        val cache = V25CacheStore(db)

        // Prime: the first read lands the PARSING snapshot in the projection.
        val priming = repo.getProject("p-1")
        assertTrue(priming is V25Result.Success)
        assertEquals(V25ProjectStatus.PARSING, (priming as V25Result.Success).value.status)
        assertEquals(1, backend.detailRequestCount)

        // The server finishes the parse; a soft-TTL read still serves the cached PARSING state
        // (its background revalidate may fire, so only the served value is asserted here).
        backend.detailParsed = true
        val stale = repo.getProject("p-1")
        assertTrue(stale is V25Result.Success)
        assertEquals(V25ProjectStatus.PARSING, (stale as V25Result.Success).value.status)

        // The forced read observes server truth AND rewrites the persisted projection.
        val forced = repo.getProject("p-1", forceRefresh = true)
        assertTrue(forced is V25Result.Success)
        assertEquals(V25ProjectStatus.AWAITING_CHAPTER_CONFIRMATION, (forced as V25Result.Success).value.status)
        val projection = cache.readProject("u-1", "p-1")
        assertEquals(V25ProjectStatus.AWAITING_CHAPTER_CONFIRMATION, projection?.status)
        assertEquals(1, projection?.file?.chapters?.size)
        db.close()
    }

    // --- 2. forced read offline: the cached snapshot is the honest fallback --------------------------

    @Test
    fun test_offline_getProject_forceRefresh_offline_falls_back_to_cached_snapshot() = runBlocking {
        val db = openDatabase()
        val repo = buildStack(db, scope())

        val priming = repo.getProject("p-1")
        assertTrue(priming is V25Result.Success)
        backend.offline = true

        val forced = repo.getProject("p-1", forceRefresh = true)
        assertTrue("offline forced read must fall back to the cached snapshot", forced is V25Result.Success)
        assertEquals(V25ProjectStatus.PARSING, (forced as V25Result.Success).value.status)
        db.close()
    }

    // --- 3. forced read offline without any cache: the failure is reported, never fabricated ---------

    @Test
    fun test_offline_getProject_forceRefresh_without_cache_returns_failure() = runBlocking {
        val db = openDatabase()
        val repo = buildStack(db, scope())
        backend.offline = true

        val forced = repo.getProject("p-1", forceRefresh = true)
        assertTrue("no cache + no network must surface a Failure", forced is V25Result.Failure)
        db.close()
    }

    // --- 4. refreshParsingProjects: the reconcile trusts the detail over a stale PARSING list -------

    @Test
    fun test_offline_refreshParsingProjects_advances_parsing_projection() = runBlocking {
        val db = openDatabase()
        val repo = buildStack(db, scope())
        val cache = V25CacheStore(db)

        val priming = repo.getProject("p-1")
        assertTrue(priming is V25Result.Success)
        assertEquals(V25ProjectStatus.PARSING, (priming as V25Result.Success).value.status)

        // The list still reports PARSING, but the detail has finished: the reconcile must
        // force the detail read and fold the finished parse into the projection.
        backend.listParsed = false
        backend.detailParsed = true
        repo.refreshParsingProjects()

        val projection = cache.readProject("u-1", "p-1")
        assertEquals(
            "the reconcile must fold the finished parse into the projection",
            V25ProjectStatus.AWAITING_CHAPTER_CONFIRMATION,
            projection?.status,
        )
        db.close()
    }

    companion object {
        private fun projectBody(parsed: Boolean): String {
            val file = if (parsed) {
                """
                "file_id": "f-1", "filename": "book.pdf", "size_bytes": 1024,
                "status": "PARSED", "error_code": null,
                "chapters": [{"chapter_id": "ch-1", "name": "第一章", "start_page": 1, "end_page": 10}],
                "created_at": "2026-08-31T00:00:00Z"
                """.trimIndent()
            } else {
                """
                "file_id": "f-1", "filename": "book.pdf", "size_bytes": 1024,
                "status": "PARSING", "error_code": null, "chapters": null,
                "created_at": "2026-08-31T00:00:00Z"
                """.trimIndent()
            }
            val status = if (parsed) "AWAITING_CHAPTER_CONFIRMATION" else "PARSING"
            val chapterCount = if (parsed) 1 else 0
            return """
                {"project_id": "p-1", "name": "测试项目",
                 "file": {$file},
                 "status": "$status", "chapter_count": $chapterCount,
                 "deck_count": 0, "task_count": 0,
                 "created_at": "2026-08-31T00:00:00Z", "updated_at": "2026-08-31T00:00:00Z"}
            """.trimIndent()
        }
    }
}
