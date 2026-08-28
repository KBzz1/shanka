package com.qiuzhao.flashcards.ui

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsSelected
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithContentDescription
import androidx.compose.ui.test.performClick
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test

class ProjectDeletionDialogTest {
    @get:Rule val rule = createComposeRule()

    @Test fun deletionConfirmationDefaultsToKeepingDecksAndSendsTheSelectedChoice() {
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

        rule.onNodeWithContentDescription("保留卡组和卡片").assertIsDisplayed().assertIsSelected()
        rule.onNodeWithContentDescription("同时删除卡组和卡片").performClick().assertIsSelected()
        rule.onNodeWithContentDescription("确认删除项目").performClick()

        rule.runOnIdle { assertEquals(false, retainDecks) }
    }
}
