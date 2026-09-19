package com.qiuzhao.flashcards.ui

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * 学习界面删卡后的会话队列回归：删掉的卡连同它的复练重进项一起出队，落位到"下一张"；
 * 队列清空走完成页；对已不在当前位次之前的卡保持幂等。
 */
class StudyCardDeletionTest {

    private fun entry(id: String, relearn: Boolean = false) = StudyQueueEntry(id, relearn)

    @Test
    fun `deleting a middle card lands on the next one`() {
        val queue = listOf(entry("a"), entry("b"), entry("c"))

        val (remaining, index) = deletedCardQueueState(queue, entryIndex = 1, cardId = "b")!!

        assertEquals(listOf("a", "c"), remaining.map { it.cardId })
        assertEquals(1, index)
    }

    @Test
    fun `deleting the last card clamps back to the new tail`() {
        val queue = listOf(entry("a"), entry("b"), entry("c"))

        val (remaining, index) = deletedCardQueueState(queue, entryIndex = 2, cardId = "c")!!

        assertEquals(listOf("a", "b"), remaining.map { it.cardId })
        assertEquals(1, index)
    }

    @Test
    fun `deleting the only card exhausts the session`() {
        assertNull(deletedCardQueueState(listOf(entry("a")), entryIndex = 0, cardId = "a"))
    }

    @Test
    fun `pending relearn revisit of the deleted card leaves with it`() {
        val queue = listOf(entry("a"), entry("b"), entry("b", relearn = true), entry("c"))

        val (remaining, index) = deletedCardQueueState(queue, entryIndex = 1, cardId = "b")!!

        assertEquals(listOf("a", "c"), remaining.map { it.cardId })
        assertEquals(1, index)
    }

    @Test
    fun `relearn revisit ahead of an earlier position is dropped with correct landing`() {
        val queue = listOf(entry("a"), entry("b"), entry("c"), entry("a", relearn = true))

        val (remaining, index) = deletedCardQueueState(queue, entryIndex = 0, cardId = "a")!!

        assertEquals(listOf("b", "c"), remaining.map { it.cardId })
        assertEquals(0, index)
    }

    @Test
    fun `deleting a card absent at or before the position is a no-op`() {
        val queue = listOf(entry("a"), entry("b"))

        val (remaining, index) = deletedCardQueueState(queue, entryIndex = 1, cardId = "z")!!

        assertEquals(queue, remaining)
        assertEquals(1, index)
    }
}
