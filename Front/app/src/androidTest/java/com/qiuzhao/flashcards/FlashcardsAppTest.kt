package com.qiuzhao.flashcards

import android.app.Application
import android.content.Context
import androidx.compose.ui.test.ExperimentalTestApi
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.test.platform.app.InstrumentationRegistry
import com.qiuzhao.flashcards.data.remote.ApiResult
import com.qiuzhao.flashcards.data.remote.Dashboard
import com.qiuzhao.flashcards.data.remote.DeckSummary
import com.qiuzhao.flashcards.data.remote.RemoteFlashcardRepository
import com.qiuzhao.flashcards.data.session.InMemorySessionStore
import com.qiuzhao.flashcards.data.session.KeystoreSessionStore
import com.qiuzhao.flashcards.data.session.SessionStore
import com.qiuzhao.flashcards.data.session.SessionUser
import com.qiuzhao.flashcards.ui.AppViewModel
import org.json.JSONObject
import org.junit.Before
import org.junit.Rule
import org.junit.Test

/**
 * 注入缝测试替身：启动校验（refreshMe）与启动即触发的业务刷新（refreshDecks/dashboard）
 * 全部返回 mock 结果，测试全程不发任何网络请求，不依赖本机后端是否启动。
 */
private class FakeFlashcardRepository(
    context: Context,
    sessionStore: SessionStore
) : RemoteFlashcardRepository(context, sessionStore = sessionStore) {
    override suspend fun refreshMe(): ApiResult<SessionUser> =
        ApiResult.Success(SessionUser("user-1", "alice", "2026-08-14T00:00:00Z"))

    override suspend fun refreshDecks(): ApiResult<List<DeckSummary>> = ApiResult.Success(emptyList())

    override suspend fun dashboard(weeklyGoal: Int?): ApiResult<Dashboard> =
        ApiResult.Success(Dashboard(false, null, 0, null, JSONObject()))
}

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
        // 注入缝版：会话与仓库双注入，启动校验走 fake（refreshMe 直接成功）——
        // backend 401/网络失败走 mock 路径，断言不依赖本机后端启动状态。
        val application = context.applicationContext as Application
        val store = InMemorySessionStore().apply {
            save(
                "test-token",
                SessionUser(userId = "user-1", username = "alice", createdAt = "2026-08-14T00:00:00Z")
            )
        }
        val viewModel = AppViewModel(
            application,
            sessionStore = store,
            repository = FakeFlashcardRepository(application, store)
        )
        rule.setContent { ShankaRoot(viewModel) }
        rule.waitUntilAtLeastOneExists(hasText("今日目标"), timeoutMillis = 10_000)
        rule.onNodeWithText("今日目标").assertIsDisplayed()
        rule.onNodeWithText("用户名，快来学习").assertIsDisplayed()
    }
}
