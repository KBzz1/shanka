package com.qiuzhao.flashcards.data.remote

import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class RequestPacerTest {

    @Test
    fun `pacer spaces six request starts beyond the five per second burst`() = runBlocking {
        var now = 0L
        val waits = mutableListOf<Long>()
        val starts = mutableListOf<Long>()
        val pacer = RequestPacer(
            minIntervalMs = 220L,
            nowMs = { now },
            sleeper = { wait ->
                waits += wait
                now += wait
            },
        )

        repeat(6) {
            pacer.awaitSlot()
            starts += now
        }

        assertEquals(listOf(0L, 220L, 440L, 660L, 880L, 1_100L), starts)
        assertEquals(listOf(220L, 220L, 220L, 220L, 220L), waits)
        assertTrue(starts.zipWithNext().all { (before, after) -> after - before >= 220L })
    }

    @Test
    fun `pacer does not delay when a slow request already crossed the next slot`() = runBlocking {
        var now = 10_000L
        val waits = mutableListOf<Long>()
        val pacer = RequestPacer(
            minIntervalMs = 220L,
            nowMs = { now },
            sleeper = { wait ->
                waits += wait
                now += wait
            },
        )

        pacer.awaitSlot()
        now += 500L // the preceding network request took longer than the pacing interval
        pacer.awaitSlot()

        assertEquals(emptyList<Long>(), waits)
    }
}
