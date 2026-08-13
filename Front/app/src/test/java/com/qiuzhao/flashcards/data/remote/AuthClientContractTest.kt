package com.qiuzhao.flashcards.data.remote

import com.qiuzhao.flashcards.data.session.Session
import com.qiuzhao.flashcards.data.session.SessionUser
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Locks the auth network contract on the JVM: header derivation, response parsing and the
 * 401 semantics. The HttpURLConnection transport itself needs the Android runtime and is
 * verified on device; these pure functions are the request construction it delegates to.
 */
class AuthClientContractTest {

    private val user = SessionUser(userId = "user-1", username = "alice", createdAt = "2026-08-14T00:00:00Z")
    private val session = Session(token = "token-1", user = user)

    @Test fun `buildAuthHeaders adds Bearer only, never a device id`() {
        val headers = buildAuthHeaders("token-1")
        assertEquals(mapOf("Authorization" to "Bearer token-1"), headers)
        assertFalse(headers.containsKey("X-Device-ID"))
    }

    @Test fun `buildAuthHeaders without a token is empty`() {
        val headers = buildAuthHeaders(null)
        assertTrue(headers.isEmpty())
        assertFalse(headers.containsKey("X-Device-ID"))
    }

    @Test fun `signed in requests carry the stored session token`() {
        assertEquals("token-1", requestAuthToken(authenticate = true, tokenOverride = null, session = session))
    }

    @Test fun `register and login never carry a bearer token, even when signed in`() {
        assertNull(requestAuthToken(authenticate = false, tokenOverride = null, session = session))
    }

    @Test fun `logout carries the explicit token even without a stored session`() {
        assertEquals("token-2", requestAuthToken(authenticate = true, tokenOverride = "token-2", session = null))
    }

    @Test fun `signed out requests carry no token`() {
        assertNull(requestAuthToken(authenticate = true, tokenOverride = null, session = null))
    }

    @Test fun `parseSession reads the register and login response shape`() {
        assertEquals(session, parseSession(JSONObject(sessionBody())))
    }

    @Test fun `parseSession returns null without an access token`() {
        val value = JSONObject("""{"user": {"user_id": "user-1", "username": "alice", "created_at": "2026-08-14T00:00:00Z"}}""")
        assertNull(parseSession(value))
    }

    @Test fun `parseSession returns null without a user`() {
        assertNull(parseSession(JSONObject("""{"access_token": "token-1"}""")))
    }

    @Test fun `parseSessionUser reads the me response user shape`() {
        val value = JSONObject("""{"user_id": "user-1", "username": "alice", "created_at": "2026-08-14T00:00:00Z"}""")
        assertEquals(user, parseSessionUser(value))
    }

    @Test fun `parseSessionUser returns null when the username is missing`() {
        assertNull(parseSessionUser(JSONObject("""{"user_id": "user-1"}""")))
    }

    @Test fun `decode turns a 201 auth body into a session`() {
        val result = HttpResult(201, sessionBody(), emptyMap()).decode { parseSession(it) ?: error("missing") }
        assertEquals(ApiResult.Success(session), result)
    }

    @Test fun `decode turns a 401 auth error into a coded failure`() {
        val body = """{"error": {"code": "AUTH_INVALID", "message": "会话无效", "localization_key": "auth.invalid"}}"""
        val result = HttpResult(401, body, emptyMap()).decode { }
        assertEquals(ApiResult.Failure(401, "AUTH_INVALID", "auth.invalid", "会话无效"), result)
    }

    @Test fun `decode reads AUTH_REQUIRED and INVALID_CREDENTIALS codes`() {
        assertEquals(
            ApiResult.Failure(401, "AUTH_REQUIRED", null, "缺少 Bearer 凭证"),
            HttpResult(401, """{"error": {"code": "AUTH_REQUIRED", "message": "缺少 Bearer 凭证"}}""", emptyMap()).decode { }
        )
        assertEquals(
            ApiResult.Failure(401, "INVALID_CREDENTIALS", null, "用户名或密码错误"),
            HttpResult(401, """{"error": {"code": "INVALID_CREDENTIALS", "message": "用户名或密码错误"}}""", emptyMap()).decode { }
        )
    }

    @Test fun `only auth 401s clear the session, never credential or network failures`() {
        assertTrue(ApiResult.Failure(401, "AUTH_REQUIRED", null, null).isAuthFailure())
        assertTrue(ApiResult.Failure(401, "AUTH_INVALID", null, null).isAuthFailure())
        assertFalse(ApiResult.Failure(401, "INVALID_CREDENTIALS", null, null).isAuthFailure())
        assertFalse(ApiResult.Failure(503, "NETWORK_UNAVAILABLE", null, null).isAuthFailure())
    }

    private fun sessionBody(): String =
        """{"user": {"user_id": "user-1", "username": "alice", "created_at": "2026-08-14T00:00:00Z"}, "access_token": "token-1", "token_type": "Bearer", "expires_at": "2026-08-21T00:00:00Z"}"""
}
