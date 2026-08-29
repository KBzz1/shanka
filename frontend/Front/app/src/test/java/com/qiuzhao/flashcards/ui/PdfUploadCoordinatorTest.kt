package com.qiuzhao.flashcards.ui

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Locks the PDF upload coordinator's replay semantics on the JVM: one user upload owns a fixed
 * Idempotency-Key, a retry of the same file replays the identical multipart request, a changed
 * file/name starts fresh, and re-entry while a request is in flight is rejected.
 */
class PdfUploadCoordinatorTest {

    private fun attempt(coordinator: PdfUploadCoordinator, operation: PdfUploadOperation, uri: String, fileName: String) =
        coordinator.begin(operation, uri, fileName)

    @Test
    fun `a fresh upload gets a fixed idempotency key and marks the coordinator uploading`() {
        val coordinator = PdfUploadCoordinator()

        val started = attempt(coordinator, PdfUploadOperation.CreateProject("数学"), "content://a.pdf", "a.pdf")

        assertNotNull(started)
        assertTrue("every upload owns its idempotency key", started!!.idempotencyKey.isNotBlank())
        assertTrue(coordinator.uploading.value)
        assertEquals(PdfUploadOperation.CreateProject("数学"), coordinator.attempt.value?.operation)
    }

    @Test
    fun `a failed upload keeps the attempt and a retry of the same file reuses the key`() {
        val coordinator = PdfUploadCoordinator()

        val first = attempt(coordinator, PdfUploadOperation.CreateProject(null), "content://a.pdf", "a.pdf")!!
        coordinator.fail()

        val retried = attempt(coordinator, PdfUploadOperation.CreateProject(null), "content://a.pdf", "a.pdf")
        assertEquals(
            "the retry replays the identical multipart request with the same key",
            first.idempotencyKey,
            retried!!.idempotencyKey,
        )
        assertEquals("a failed attempt stays until committed", retried, coordinator.attempt.value)
    }

    @Test
    fun `a retry with a different file or changed name starts a fresh key`() {
        val coordinator = PdfUploadCoordinator()

        val first = attempt(coordinator, PdfUploadOperation.CreateProject(null), "content://a.pdf", "a.pdf")!!
        coordinator.fail()

        val otherFile = attempt(coordinator, PdfUploadOperation.CreateProject(null), "content://b.pdf", "b.pdf")!!
        assertNotEquals("a different file is a different operation", first.idempotencyKey, otherFile.idempotencyKey)
        coordinator.fail()

        val changedName = attempt(coordinator, PdfUploadOperation.CreateProject("新项目名"), "content://a.pdf", "a.pdf")!!
        assertNotEquals("a changed project name is a different operation", first.idempotencyKey, changedName.idempotencyKey)
    }

    @Test
    fun `creating and replacing are distinct operations with distinct keys`() {
        val coordinator = PdfUploadCoordinator()

        val created = attempt(coordinator, PdfUploadOperation.CreateProject(null), "content://a.pdf", "a.pdf")!!
        coordinator.fail()

        val replaced = attempt(coordinator, PdfUploadOperation.ReplacePdf("project-1"), "content://a.pdf", "a.pdf")!!
        assertEquals(PdfUploadOperation.ReplacePdf("project-1"), replaced.operation)
        assertNotEquals("create and replace must never share a key", created.idempotencyKey, replaced.idempotencyKey)
    }

    @Test
    fun `a begin during an upload is rejected`() {
        val coordinator = PdfUploadCoordinator()

        attempt(coordinator, PdfUploadOperation.CreateProject(null), "content://a.pdf", "a.pdf")
        assertTrue(coordinator.uploading.value)

        assertNull("a second upload while one is in flight is rejected", attempt(coordinator, PdfUploadOperation.ReplacePdf("project-1"), "content://a.pdf", "a.pdf"))
        assertNull("even the same file is rejected while a request is in flight", attempt(coordinator, PdfUploadOperation.CreateProject(null), "content://a.pdf", "a.pdf"))
    }

    @Test
    fun `a committed upload clears the attempt so the next upload starts fresh`() {
        val coordinator = PdfUploadCoordinator()

        val first = attempt(coordinator, PdfUploadOperation.ReplacePdf("project-1"), "content://a.pdf", "a.pdf")!!
        coordinator.commit()

        assertNull("a committed upload releases its attempt", coordinator.attempt.value)
        assertEquals(false, coordinator.uploading.value)

        val second = attempt(coordinator, PdfUploadOperation.ReplacePdf("project-1"), "content://a.pdf", "a.pdf")!!
        assertNotEquals("the next upload is a fresh operation", first.idempotencyKey, second.idempotencyKey)
    }
}
