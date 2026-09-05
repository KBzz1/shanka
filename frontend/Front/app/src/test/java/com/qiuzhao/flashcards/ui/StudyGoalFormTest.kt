package com.qiuzhao.flashcards.ui

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Pure gates of the Figma 977:4937 plan page: the save button must stay off
 * until a first-time user has picked a project and a learnable deck, and the
 * 72dp wheel must shrink three-digit values into their slot.
 */
class StudyGoalFormTest {

    @Test
    fun `configured user saves with valid seeded goals alone`() {
        assertTrue(
            studyGoalCanSave(
                configured = true, seeded = true, saving = false,
                validGoals = true, hasProject = false, hasLearnableSelection = false,
            )
        )
    }

    @Test
    fun `first configuration requires a project and a learnable deck selection`() {
        assertFalse(
            studyGoalCanSave(
                configured = false, seeded = true, saving = false,
                validGoals = true, hasProject = false, hasLearnableSelection = true,
            )
        )
        assertFalse(
            studyGoalCanSave(
                configured = false, seeded = true, saving = false,
                validGoals = true, hasProject = true, hasLearnableSelection = false,
            )
        )
        assertTrue(
            studyGoalCanSave(
                configured = false, seeded = true, saving = false,
                validGoals = true, hasProject = true, hasLearnableSelection = true,
            )
        )
    }

    @Test
    fun `unsaved in-flight or unseeded or invalid goal states never save`() {
        assertFalse(
            studyGoalCanSave(
                configured = true, seeded = false, saving = false,
                validGoals = true, hasProject = true, hasLearnableSelection = true,
            )
        )
        assertFalse(
            studyGoalCanSave(
                configured = true, seeded = true, saving = true,
                validGoals = true, hasProject = true, hasLearnableSelection = true,
            )
        )
        assertFalse(
            studyGoalCanSave(
                configured = true, seeded = true, saving = false,
                validGoals = false, hasProject = true, hasLearnableSelection = true,
            )
        )
    }

    @Test
    fun `one and two digit wheel values keep full tier size`() {
        assertEquals(1f, wheelFontScale(0), 0f)
        assertEquals(1f, wheelFontScale(40), 0f)
        assertEquals(1f, wheelFontScale(90), 0f)
    }

    @Test
    fun `three digit wheel values shrink into the 64dp slot`() {
        assertEquals(0.68f, wheelFontScale(100), 0f)
        assertEquals(0.68f, wheelFontScale(200), 0f)
    }
}
