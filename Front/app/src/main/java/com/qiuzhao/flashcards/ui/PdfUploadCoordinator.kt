package com.qiuzhao.flashcards.ui

import java.util.UUID
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * One PDF upload operation. The operation kind captures the server path (create project vs.
 * replace the project's PDF); the retry identity of one user submission is the operation plus the
 * exact file (uri + display name), so an unchanged retry reuses the fixed Idempotency-Key while a
 * new file or a changed name starts a fresh key.
 */
sealed interface PdfUploadOperation {
    /** POST /projects — a null name lets the server derive the project name from the file name. */
    data class CreateProject(val name: String?) : PdfUploadOperation

    /** POST /projects/{project_id}/replace-pdf — replace the only PDF after a parse failure. */
    data class ReplacePdf(val projectId: String) : PdfUploadOperation
}

/**
 * The in-flight state of one PDF upload. It owns the operation's fixed Idempotency-Key, so a
 * retry after a lost response replays the identical multipart request (the server's idempotency
 * layer keys on Idempotency-Key + path + body hash) and can never create a second project or
 * replace the wrong PDF. The attempt survives configuration changes in the ViewModel; after a
 * full process death it is gone by design and the caller refreshes server state instead.
 */
data class PdfUploadAttempt(
    val operation: PdfUploadOperation,
    val uriKey: String,
    val fileName: String,
    val idempotencyKey: String,
)

/**
 * Owns the PDF upload attempt state and the uploading flag. The stream opening and the actual
 * repository call stay with the ViewModel (Android [android.net.Uri] / ContentResolver); this
 * coordinator only picks the key, guards re-entry and remembers what to replay.
 */
class PdfUploadCoordinator {

    private val _attempt = MutableStateFlow<PdfUploadAttempt?>(null)
    val attempt: StateFlow<PdfUploadAttempt?> = _attempt.asStateFlow()

    private val _uploading = MutableStateFlow(false)
    val uploading: StateFlow<Boolean> = _uploading.asStateFlow()

    /**
     * Begins (or resumes) an upload for one user operation. Same operation, same file and same
     * display name reuses the stored attempt's Idempotency-Key; a different file or a changed
     * name starts fresh. Returns null while another upload is in flight (re-entry is rejected).
     */
    fun begin(operation: PdfUploadOperation, uriKey: String, fileName: String): PdfUploadAttempt? {
        if (_uploading.value) return null
        val attempt = _attempt.value?.takeIf {
            it.operation == operation && it.uriKey == uriKey && it.fileName == fileName
        } ?: PdfUploadAttempt(operation, uriKey, fileName, UUID.randomUUID().toString())
        _attempt.value = attempt
        _uploading.value = true
        return attempt
    }

    /** The upload committed; the next upload starts fresh. */
    fun commit() {
        _attempt.value = null
        _uploading.value = false
    }

    /** The upload failed; a retry of the same file reuses its fixed key. */
    fun fail() {
        _uploading.value = false
    }

    /** Clears any attempt, e.g. on logout. */
    fun reset() {
        _attempt.value = null
        _uploading.value = false
    }
}
