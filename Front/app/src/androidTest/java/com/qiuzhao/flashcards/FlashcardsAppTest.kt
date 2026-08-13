package com.qiuzhao.flashcards

import androidx.compose.ui.test.ExperimentalTestApi
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.test.platform.app.InstrumentationRegistry
import com.qiuzhao.flashcards.data.session.KeystoreSessionStore
import com.qiuzhao.flashcards.data.session.SessionUser
import org.junit.Before
import org.junit.Rule
import org.junit.Test

@OptIn(ExperimentalTestApi::class)
class FlashcardsAppTest {
    @get:Rule val rule = createAndroidComposeRule<MainActivity>()
    private val context get() = InstrumentationRegistry.getInstrumentation().targetContext

    @Before fun clearStoredSession() {
        KeystoreSessionStore(context).clear()
    }

    @Test fun loggedOutStartupLandsOnTheLoginScreen() {
        // T3 登录门控：无会话时不再直登主界面，而是进入登录页。
        rule.onNodeWithText("登录以同步你的卡组与学习进度").assertIsDisplayed()
        rule.onNodeWithText("还没有账号？立即注册").assertIsDisplayed()
    }

    @Test fun storedSessionEntersTheMainScreen() {
        // 注入会话后重启 Activity 触发启动校验：本机联调后端未启动时 /auth/me 快速网络失败，
        // AuthViewModel 按设计保留会话进入主界面（仅提示网络错误）。
        KeystoreSessionStore(context).save(
            "test-token",
            SessionUser(userId = "user-1", username = "alice", createdAt = "2026-08-14T00:00:00Z")
        )
        rule.activityRule.scenario.recreate()
        rule.waitUntilAtLeastOneExists(hasText("今日目标"), timeoutMillis = 10_000)
        rule.onNodeWithText("今日目标").assertIsDisplayed()
        rule.onNodeWithText("用户名，快来学习").assertIsDisplayed()
    }
}
