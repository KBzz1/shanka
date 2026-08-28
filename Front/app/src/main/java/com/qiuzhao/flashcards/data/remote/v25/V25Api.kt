package com.qiuzhao.flashcards.data.remote.v25

import com.qiuzhao.flashcards.BuildConfig
import com.qiuzhao.flashcards.data.remote.BackendClient
import com.qiuzhao.flashcards.data.remote.HttpResult
import com.qiuzhao.flashcards.data.remote.buildAuthHeaders
import com.qiuzhao.flashcards.data.remote.requestAuthToken
import com.qiuzhao.flashcards.data.session.SessionStore
import com.qiuzhao.flashcards.data.session.loadQuietly
import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.InputStream
import java.net.HttpURLConnection
import java.net.URL
import java.util.UUID
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject

/**
 * Transport seam of the V2.5 remote data tier. [RemoteV25Repository] talks only to this
 * interface, so JVM tests can record and fake every request without an Android runtime.
 * The production implementation reuses the existing bearer-session and Idempotency-Key
 * mechanisms of [BackendClient] unchanged; only the multipart uploads — which the legacy
 * client cannot express (its [BackendClient.uploadPdf] is Uri-based and pinned to `/pdfs`)
 * — are implemented here. JSON calls share the client's 429 Retry-After retry loop;
 * multipart uploads carry the same auth and idempotency headers but do NOT retry on 429
 * (deferred work item, tracked for the terminal review).
 */
interface V25Transport {
    /**
     * A JSON request. `idempotent` asks the transport to attach an Idempotency-Key header;
     * `idempotencyKey` supplies the caller's own key so one user operation can retry with the
     * exact same key after a lost response. An explicit key is always attached and takes
     * precedence over the `idempotent` flag.
     */
    suspend fun request(
        operation: String,
        method: String,
        path: String,
        body: String? = null,
        contentType: String = "application/json",
        idempotent: Boolean = true,
        authenticate: Boolean = true,
        token: String? = null,
        idempotencyKey: String? = null,
    ): HttpResult

    /**
     * A multipart POST carrying a PDF part and an optional `name` form field. `idempotencyKey`
     * fixes the caller's key for a retried upload; without one a fresh key is generated.
     */
    suspend fun upload(
        operation: String,
        path: String,
        fileName: String,
        content: InputStream,
        name: String? = null,
        idempotencyKey: String? = null,
    ): HttpResult
}

/**
 * Neutralises characters that would break the multipart frame when a file name is embedded
 * in a Content-Disposition header value: CR/LF must never reach a header line, and a double
 * quote would prematurely close the quoted value.
 */
internal fun escapeMultipartFileName(fileName: String): String =
    fileName.replace("\r", "").replace("\n", "").replace("\"", "%22")

/**
 * Production transport: delegates JSON calls to the shared [BackendClient] (same 429
 * Retry-After retry loop, same session store, same debug evidence) and performs multipart
 * uploads itself. Deliberately writes no log lines: the API key and PDF content must never
 * reach a log, and the client already records request metadata for JSON calls.
 */
class BackendV25Transport(
    private val client: BackendClient,
    private val sessionStore: SessionStore,
) : V25Transport {

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
    ): HttpResult = client.request(
        operation, method, path, body, contentType, idempotent, authenticate, token,
        idempotencyKey = idempotencyKey,
    )

    override suspend fun upload(
        operation: String,
        path: String,
        fileName: String,
        content: InputStream,
        name: String?,
        idempotencyKey: String?,
    ): HttpResult = withContext(Dispatchers.IO) {
        // Same mechanism as BackendClient.executeMultipart: a UUID v4 key per upload (or the
        // caller's fixed key for a retried upload), the stored session's bearer token, and a
        // 60s read timeout for the large payload.
        val key = idempotencyKey ?: UUID.randomUUID().toString()
        val authToken = requestAuthToken(authenticate = true, tokenOverride = null, session = sessionStore.loadQuietly())
        runCatching { executeMultipart(path, fileName, content, name, key, authToken) }
            .getOrElse { unavailableResult(it) }
    }

    private fun executeMultipart(
        path: String,
        fileName: String,
        content: InputStream,
        name: String?,
        key: String,
        authToken: String?,
    ): HttpResult {
        val boundary = "----ShankaV25${UUID.randomUUID()}"
        val connection = (URL(baseUrl().trimEnd('/') + path).openConnection() as HttpURLConnection).apply {
            requestMethod = "POST"
            connectTimeout = 8_000
            readTimeout = 60_000
            doOutput = true
            setRequestProperty("Accept", "application/json")
            buildAuthHeaders(authToken).forEach { (headerName, headerValue) -> setRequestProperty(headerName, headerValue) }
            setRequestProperty("Idempotency-Key", key)
            setRequestProperty("Content-Type", "multipart/form-data; boundary=$boundary")
        }
        return try {
            BufferedOutputStream(connection.outputStream).use { output ->
                if (name != null) {
                    output.write("--$boundary\r\n".toByteArray())
                    output.write("Content-Disposition: form-data; name=\"name\"\r\n\r\n".toByteArray())
                    output.write(name.toByteArray(Charsets.UTF_8))
                    output.write("\r\n".toByteArray())
                }
                output.write("--$boundary\r\n".toByteArray())
                output.write("Content-Disposition: form-data; name=\"file\"; filename=\"${escapeMultipartFileName(fileName)}\"\r\n".toByteArray())
                output.write("Content-Type: application/pdf\r\n\r\n".toByteArray())
                content.use { it.copyTo(output) }
                output.write("\r\n--$boundary--\r\n".toByteArray())
            }
            val status = connection.responseCode
            val stream = if (status >= 400) connection.errorStream else connection.inputStream
            val response = stream?.let { BufferedInputStream(it).bufferedReader().use { reader -> reader.readText() } }.orEmpty()
            HttpResult(status, response, connection.headerFields.filterKeys { it != null })
        } finally {
            connection.disconnect()
        }
    }

    private fun unavailableResult(error: Throwable): HttpResult =
        HttpResult(
            status = 503,
            body = JSONObject().put("error", JSONObject().put("code", "NETWORK_UNAVAILABLE")).toString(),
            headers = emptyMap(),
        )

    /** Multipart calls use the same compile-time endpoint authority as JSON calls. */
    private fun baseUrl(): String = BuildConfig.API_BASE_URL
}
