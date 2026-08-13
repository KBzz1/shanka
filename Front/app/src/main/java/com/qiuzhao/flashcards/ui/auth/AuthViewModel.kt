package com.qiuzhao.flashcards.ui.auth

import com.qiuzhao.flashcards.data.remote.ApiResult
import com.qiuzhao.flashcards.data.remote.AuthRepository
import com.qiuzhao.flashcards.data.remote.isAuthFailure
import com.qiuzhao.flashcards.data.session.Session
import com.qiuzhao.flashcards.data.session.SessionStore
import com.qiuzhao.flashcards.data.session.SessionUser
import com.qiuzhao.flashcards.data.session.loadQuietly
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

/**
 * Auth session state machine. [AuthState.LoggedOut.error] and [AuthState.LoggedIn.error]
 * carry only user-facing message text derived from server error codes; server details are
 * deliberately never exposed.
 */
sealed interface AuthState {
    data object CheckingSession : AuthState

    data class LoggedOut(val error: String? = null) : AuthState

    data class LoggedIn(val user: SessionUser, val error: String? = null) : AuthState
}

const val NETWORK_ERROR_MESSAGE = "网络错误，请稍后重试"

private fun ApiResult.Failure.authErrorMessage(): String = when (code) {
    "INVALID_CREDENTIALS" -> "用户名或密码错误"
    "USERNAME_TAKEN" -> "用户名已被占用"
    "RATE_LIMITED" -> "请求过于频繁，请稍后重试"
    else -> NETWORK_ERROR_MESSAGE
}

/**
 * A plain state holder (deliberately not an AndroidX ViewModel) so the session logic runs on
 * the JVM in unit tests. [com.qiuzhao.flashcards.ui.AppViewModel] owns one instance, passes
 * its [kotlinx.coroutines.CoroutineScope], and delegates every auth decision to it.
 */
class AuthViewModel(
    private val repository: AuthRepository,
    private val sessionStore: SessionStore,
    private val scope: CoroutineScope
) {
    private val _state = MutableStateFlow<AuthState>(AuthState.CheckingSession)
    val state: StateFlow<AuthState> = _state.asStateFlow()

    private val _submitting = MutableStateFlow(false)
    val submitting: StateFlow<Boolean> = _submitting.asStateFlow()

    /**
     * Startup flow: a stored session is verified against /auth/me — 200 enters the app,
     * an auth 401 clears the dead session, and a network failure keeps the session and
     * enters the app with a network hint instead of logging the user out.
     */
    fun checkSession() = scope.launch {
        val session = sessionStore.loadQuietly()
        if (session == null) {
            _state.value = AuthState.LoggedOut()
            return@launch
        }
        when (val result = repository.refreshMe()) {
            is ApiResult.Success -> _state.value = AuthState.LoggedIn(result.value)
            is ApiResult.Failure ->
                if (result.isAuthFailure()) {
                    runCatching { sessionStore.clear() }
                    _state.value = AuthState.LoggedOut()
                } else {
                    _state.value = AuthState.LoggedIn(session.user, error = NETWORK_ERROR_MESSAGE)
                }
        }
    }

    fun login(username: String, password: String) {
        if (username.isBlank() || password.isBlank()) return
        submit { repository.login(username.trim(), password) }
    }

    fun register(username: String, password: String) {
        if (username.isBlank() || password.isBlank()) return
        submit { repository.register(username.trim(), password) }
    }

    /** Logs out locally regardless of the server result; a dead token never blocks signing out. */
    fun logout() = scope.launch {
        repository.logout()
        runCatching { sessionStore.clear() }
        _state.value = AuthState.LoggedOut()
    }

    /**
     * Guard for business request results: only a session-death 401 (AUTH_REQUIRED/AUTH_INVALID)
     * logs the user out. Credential 401s and network failures must never clear the session, so
     * [com.qiuzhao.flashcards.ui.AppViewModel] routes every business failure through here once.
     */
    fun onBusinessFailure(result: ApiResult<*>) {
        if (result is ApiResult.Failure && result.isAuthFailure()) {
            runCatching { sessionStore.clear() }
            _state.value = AuthState.LoggedOut()
        }
    }

    fun clearError() {
        when (val current = _state.value) {
            is AuthState.LoggedIn -> _state.value = current.copy(error = null)
            is AuthState.LoggedOut -> _state.value = AuthState.LoggedOut()
            is AuthState.CheckingSession -> Unit
        }
    }

    private fun submit(call: suspend () -> ApiResult<Session>) {
        if (_submitting.value) return
        scope.launch {
            _submitting.value = true
            _state.value = AuthState.LoggedOut()
            when (val result = call()) {
                is ApiResult.Success -> _state.value = AuthState.LoggedIn(result.value.user)
                is ApiResult.Failure -> _state.value = AuthState.LoggedOut(error = result.authErrorMessage())
            }
            _submitting.value = false
        }
    }
}
