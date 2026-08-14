package com.qiuzhao.flashcards

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.lifecycle.viewmodel.compose.viewModel
import com.qiuzhao.flashcards.ui.AppViewModel
import com.qiuzhao.flashcards.ui.AutumnFlashcardsTheme
import com.qiuzhao.flashcards.ui.FlashcardsApp
import com.qiuzhao.flashcards.ui.auth.AuthLoadingScreen
import com.qiuzhao.flashcards.ui.auth.AuthScreen
import com.qiuzhao.flashcards.ui.auth.AuthState

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.Q) {
            window.isNavigationBarContrastEnforced = false
        }
        setContent {
            // AppViewModel 注入缝走显式工厂（AndroidViewModelFactory 只认单参数 (Application) 构造）。
            ShankaRoot(viewModel(factory = AppViewModel.Factory))
        }
    }
}

/**
 * 启动路由根组合：会话校验中 → 占位屏；未登录 → 登录/注册页；已登录 → 既有主界面导航。
 * 从 MainActivity 抽出，instrumented 测试可用注入的 AppViewModel 直接渲染同一启动路由。
 */
@Composable
fun ShankaRoot(appViewModel: AppViewModel) {
    val darkPreference by appViewModel.darkTheme.collectAsState()
    val dark = darkPreference ?: isSystemInDarkTheme()
    AutumnFlashcardsTheme(dark = dark) {
        val authState by appViewModel.authState.collectAsState()
        when (authState) {
            is AuthState.CheckingSession -> AuthLoadingScreen()
            is AuthState.LoggedOut -> AuthScreen(appViewModel.auth)
            is AuthState.LoggedIn -> {
                // 启动校验网络失败时保留会话进入主界面，仅以 snackbar 提示网络错误。
                val error = (authState as AuthState.LoggedIn).error
                val snackbarHostState = remember { SnackbarHostState() }
                LaunchedEffect(error) {
                    if (error != null) {
                        // showSnackbar 挂起到提示消失；在此之前清 error 会取消本效果导致提示一闪而过。
                            snackbarHostState.showSnackbar(error)
                            appViewModel.clearAuthError()
                    }
                }
                Box(Modifier.fillMaxSize()) {
                    FlashcardsApp(appViewModel)
                    SnackbarHost(snackbarHostState, Modifier.align(Alignment.BottomCenter))
                }
            }
        }
    }
}
