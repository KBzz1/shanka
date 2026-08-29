package com.qiuzhao.flashcards.ui

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test

class ProjectDeletionDialogTest {
    @get:Rule val rule = createComposeRule()

    @Test fun deletionConfirmationOffersOnlyTheTwoDeckRetentionChoices() {
        var retainDecks: Boolean? = null
        rule.setContent {
            AutumnFlashcardsTheme {
                ProjectDeletionDialog(
                    projectName = "概率论",
                    theme = DeckThemes.first(),
                    onConfirm = { retainDecks = it },
                    onDismiss = {}
                )
            }
        }

        rule.onNodeWithText("删除项目，保留卡组").assertIsDisplayed()
        rule.onNodeWithText("删除项目及卡组").performClick()

        rule.runOnIdle { assertEquals(false, retainDecks) }
    }
}
