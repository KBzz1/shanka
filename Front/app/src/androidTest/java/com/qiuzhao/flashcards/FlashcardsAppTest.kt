package com.qiuzhao.flashcards

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithText
import org.junit.Rule
import org.junit.Test

class FlashcardsAppTest {
    @get:Rule val rule = createAndroidComposeRule<MainActivity>()

    @Test fun homeShowsRealHomeState() {
        // Remote-first startup may correctly have no decks yet. Assert fixed home chrome and
        // that the removed fake "计算机网络" fallback deck is gone; the greeting now carries
        // the real account nickname and a deck-less home shows the true empty state instead.
        rule.onNodeWithText("今日目标").assertIsDisplayed()
        rule.onNodeWithText("计算机网络").assertDoesNotExist()
    }
}
