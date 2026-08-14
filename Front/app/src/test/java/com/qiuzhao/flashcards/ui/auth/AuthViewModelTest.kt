package com.qiuzhao.flashcards.ui.auth

import com.qiuzhao.flashcards.data.remote.ApiResult
import com.qiuzhao.flashcards.data.remote.AuthRepository
import com.qiuzhao.flashcards.data.session.InMemorySessionStore
import com.qiuzhao.flashcards.data.session.Session
import com.qiuzhao.flashcards.data.session.SessionUser
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Locks the auth session state machine on the JVM: startup /auth/me branching, login/register
 * transitions, error-code message mapping, and the business-request 401 semantics. The
 * [AuthViewModel] is a plain state holder (not an AndroidX ViewModel) so it needs no Android
 * runtime; [AppViewModel] owns one per application and delegates to it.
 */
class AuthViewModelTest {

    private val user = SessionUser(userId = "user-1", username = "alice", createdAt = "2026-08-14T00:00:00Z")
    private val session = Session(token = "token-1", user = user)

    private class FakeAuthRepository : AuthRepository {
        var meResult: ApiResult<SessionUser> = ApiResult.Success(SessionUser("user-1", "alice", "2026-08-14T00:00:00Z"))
        var loginResult: ApiResult<Session> = ApiResult.Success(Session("token-1", SessionUser("user-1", "alice", "2026-08-14T00:00:00Z")))
        var registerResult: ApiResult<Session> = ApiResult.Success(Session("token-2", SessionUser("user-2", "bob", "2026-08-14T00:00:00Z")))
        var logoutResult: ApiResult<Unit> = ApiResult.Success(Unit)
        var loginHook: (suspend () -> ApiResult<Session>)? = null
        var logoutHook: (suspend (String) -> ApiResult<Unit>)? = null
        var revokedToken: String? = null

        override suspend fun register(username: String, password: String): ApiResult<Session> = registerResult
        override suspend fun login(username: String, password: String): ApiResult<Session> = loginHook?.invoke() ?: loginResult
        override suspend fun refreshMe(): ApiResult<SessionUser> = meResult
        override suspend fun logout(token: String): ApiResult<Unit> {
            revokedToken = token
            return logoutHook?.invoke(token) ?: logoutResult
        }
    }

    private fun failure(status: Int, code: String) = ApiResult.Failure(status, code, null, null)

    @Test fun `checkSession without a stored session stays logged out`() = runTest {
        val viewModel = AuthViewModel(FakeAuthRepository(), InMemorySessionStore(), this)

        viewModel.checkSession()
        advanceUntilIdle()

        assertEquals(AuthState.LoggedOut(), viewModel.state.value)
    }

    @Test fun `checkSession logs in from the me response when a session is stored`() = runTest {
        val store = InMemorySessionStore().apply { save(session.token, session.user) }
        val repository = FakeAuthRepository().apply { meResult = ApiResult.Success(user) }
        val viewModel = AuthViewModel(repository, store, this)

        viewModel.checkSession()
        advanceUntilIdle()

        assertEquals(AuthState.LoggedIn(user), viewModel.state.value)
        assertNotNull(store.load())
    }

    @Test fun `checkSession clears a dead session on an auth 401`() = runTest {
        val store = InMemorySessionStore().apply { save(session.token, session.user) }
        val repository = FakeAuthRepository().apply { meResult = failure(401, "AUTH_INVALID") }
        val viewModel = AuthViewModel(repository, store, this)

        viewModel.checkSession()
        advanceUntilIdle()

        assertEquals(AuthState.LoggedOut(), viewModel.state.value)
        assertNull(store.load())
    }

    @Test fun `checkSession keeps the session on a network failure instead of logging out`() = runTest {
        val store = InMemorySessionStore().apply { save(session.token, session.user) }
        val repository = FakeAuthRepository().apply { meResult = failure(503, "NETWORK_UNAVAILABLE") }
        val viewModel = AuthViewModel(repository, store, this)

        viewModel.checkSession()
        advanceUntilIdle()

        assertEquals(AuthState.LoggedIn(user, error = "网络错误，请稍后重试"), viewModel.state.value)
        assertNotNull(store.load())
    }

    @Test fun `login success transitions to logged in`() = runTest {
        val viewModel = AuthViewModel(FakeAuthRepository(), InMemorySessionStore(), this)

        viewModel.login("alice", "secret")
        advanceUntilIdle()

        assertEquals(AuthState.LoggedIn(user), viewModel.state.value)
    }

    @Test fun `login maps INVALID_CREDENTIALS to its message and stays logged out`() = runTest {
        val store = InMemorySessionStore()
        val repository = FakeAuthRepository().apply { loginResult = failure(401, "INVALID_CREDENTIALS") }
        val viewModel = AuthViewModel(repository, store, this)

        viewModel.login("alice", "wrong")
        advanceUntilIdle()

        assertEquals(AuthState.LoggedOut(error = "用户名或密码错误"), viewModel.state.value)
        assertNull(store.load())
    }

    @Test fun `login maps a network failure to the network message without touching the session`() = runTest {
        val repository = FakeAuthRepository().apply { loginResult = failure(503, "NETWORK_UNAVAILABLE") }
        val viewModel = AuthViewModel(repository, InMemorySessionStore(), this)

        viewModel.login("alice", "secret")
        advanceUntilIdle()

        assertEquals(AuthState.LoggedOut(error = "网络错误，请稍后重试"), viewModel.state.value)
    }

    @Test fun `register success transitions to logged in`() = runTest {
        val viewModel = AuthViewModel(FakeAuthRepository(), InMemorySessionStore(), this)

        viewModel.register("bob", "secret")
        advanceUntilIdle()

        assertEquals(AuthState.LoggedIn(SessionUser("user-2", "bob", "2026-08-14T00:00:00Z")), viewModel.state.value)
    }

    @Test fun `register maps USERNAME_TAKEN to its message`() = runTest {
        val repository = FakeAuthRepository().apply { registerResult = failure(409, "USERNAME_TAKEN") }
        val viewModel = AuthViewModel(repository, InMemorySessionStore(), this)

        viewModel.register("bob", "secret")
        advanceUntilIdle()

        assertEquals(AuthState.LoggedOut(error = "用户名已被占用"), viewModel.state.value)
    }

    @Test fun `register maps RATE_LIMITED to its message`() = runTest {
        val repository = FakeAuthRepository().apply { registerResult = failure(429, "RATE_LIMITED") }
        val viewModel = AuthViewModel(repository, InMemorySessionStore(), this)

        viewModel.register("bob", "secret")
        advanceUntilIdle()

        assertEquals(AuthState.LoggedOut(error = "请求过于频繁，请稍后重试"), viewModel.state.value)
    }

    @Test fun `blank credentials are rejected before any request is made`() = runTest {
        val repository = FakeAuthRepository()
        val viewModel = AuthViewModel(repository, InMemorySessionStore(), this)

        viewModel.login("alice", "")
        viewModel.register("", "secret")
        advanceUntilIdle()

        assertEquals(AuthState.CheckingSession, viewModel.state.value)
        assertEquals(ApiResult.Success(session), repository.loginResult)
    }

    @Test fun `a business auth 401 clears the session and returns to logged out`() = runTest {
        val store = InMemorySessionStore().apply { save(session.token, session.user) }
        val repository = FakeAuthRepository().apply { meResult = ApiResult.Success(user) }
        val viewModel = AuthViewModel(repository, store, this)
        viewModel.checkSession()
        advanceUntilIdle()
        assertEquals(AuthState.LoggedIn(user), viewModel.state.value)

        viewModel.onBusinessFailure(failure(401, "AUTH_REQUIRED"))
        advanceUntilIdle()

        assertEquals(AuthState.LoggedOut(), viewModel.state.value)
        assertNull(store.load())
    }

    @Test fun `an INVALID_CREDENTIALS 401 never clears the session`() = runTest {
        val store = InMemorySessionStore().apply { save(session.token, session.user) }
        val repository = FakeAuthRepository().apply { meResult = ApiResult.Success(user) }
        val viewModel = AuthViewModel(repository, store, this)
        viewModel.checkSession()
        advanceUntilIdle()

        viewModel.onBusinessFailure(failure(401, "INVALID_CREDENTIALS"))
        viewModel.onBusinessFailure(failure(503, "NETWORK_UNAVAILABLE"))
        advanceUntilIdle()

        assertEquals(AuthState.LoggedIn(user), viewModel.state.value)
        assertNotNull(store.load())
    }

    @Test fun `logout clears the session and returns to logged out`() = runTest {
        val store = InMemorySessionStore().apply { save(session.token, session.user) }
        val viewModel = AuthViewModel(FakeAuthRepository(), store, this)

        viewModel.logout()
        advanceUntilIdle()

        assertEquals(AuthState.LoggedOut(), viewModel.state.value)
        assertNull(store.load())
    }

    @Test fun `logout clears local session before network revocation`() = runTest {
        // fake repository：logout 挂起不返回（模拟断网）；本地登出必须不等网络返回。
        val gate = CompletableDeferred<Unit>()
        val store = InMemorySessionStore().apply { save(session.token, session.user) }
        val repository = FakeAuthRepository().apply { logoutHook = { gate.await(); ApiResult.Success(Unit) } }
        val viewModel = AuthViewModel(repository, store, backgroundScope)

        viewModel.logout()
        runCurrent()

        assertEquals(AuthState.LoggedOut(), viewModel.state.value)
        assertNull(store.load())
        // 网络撤销以清空前捕获的 token 发起（store 已空，不复读）。
        assertEquals("token-1", repository.revokedToken)
    }

    @Test fun `submitting is true while a request is in flight`() = runTest {
        val gate = CompletableDeferred<Unit>()
        val repository = FakeAuthRepository().apply { loginHook = { gate.await(); ApiResult.Success(session) } }
        val viewModel = AuthViewModel(repository, InMemorySessionStore(), this)

        viewModel.login("alice", "secret")
        runCurrent()
        assertTrue(viewModel.submitting.value)
        gate.complete(Unit)
        advanceUntilIdle()

        assertFalse(viewModel.submitting.value)
        assertEquals(AuthState.LoggedIn(user), viewModel.state.value)
    }
}
