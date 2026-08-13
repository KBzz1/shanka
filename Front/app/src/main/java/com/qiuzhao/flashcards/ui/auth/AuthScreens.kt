package com.qiuzhao.flashcards.ui.auth

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.LocalContentColor
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp

/** 启动会话校验期间的占位屏；校验完成后由 MainActivity 切换到登录页或主界面。 */
@Composable
fun AuthLoadingScreen() {
    Box(
        Modifier.fillMaxSize().background(MaterialTheme.colorScheme.background),
        contentAlignment = Alignment.Center
    ) {
        CircularProgressIndicator()
    }
}

/**
 * Auth 入口：在登录/注册两页之间切换。密码输入只存内存（remember，不落 Bundle/磁盘），
 * 切换模式时清空；用户名用 rememberSaveable 保留便于纠错重输。
 */
@Composable
fun AuthScreen(auth: AuthViewModel) {
    var registerMode by rememberSaveable { mutableStateOf(false) }
    var username by rememberSaveable { mutableStateOf("") }
    var password by remember { mutableStateOf("") }
    val state by auth.state.collectAsState()
    val submitting by auth.submitting.collectAsState()
    val error = (state as? AuthState.LoggedOut)?.error

    val switchMode = {
        registerMode = !registerMode
        password = ""
        auth.clearError()
    }

    if (registerMode) {
        RegisterScreen(
            username = username,
            onUsernameChange = { username = it },
            password = password,
            onPasswordChange = { password = it },
            error = error,
            submitting = submitting,
            onSubmit = { auth.register(username, password) },
            onSwitchToLogin = switchMode
        )
    } else {
        LoginScreen(
            username = username,
            onUsernameChange = { username = it },
            password = password,
            onPasswordChange = { password = it },
            error = error,
            submitting = submitting,
            onSubmit = { auth.login(username, password) },
            onSwitchToRegister = switchMode
        )
    }
}

@Composable
fun LoginScreen(
    username: String,
    onUsernameChange: (String) -> Unit,
    password: String,
    onPasswordChange: (String) -> Unit,
    error: String?,
    submitting: Boolean,
    onSubmit: () -> Unit,
    onSwitchToRegister: () -> Unit
) {
    AuthForm(
        subtitle = "登录以同步你的卡组与学习进度",
        submitLabel = "登录",
        switchLabel = "还没有账号？立即注册",
        username = username,
        onUsernameChange = onUsernameChange,
        password = password,
        onPasswordChange = onPasswordChange,
        error = error,
        submitting = submitting,
        onSubmit = onSubmit,
        onSwitch = onSwitchToRegister
    )
}

@Composable
fun RegisterScreen(
    username: String,
    onUsernameChange: (String) -> Unit,
    password: String,
    onPasswordChange: (String) -> Unit,
    error: String?,
    submitting: Boolean,
    onSubmit: () -> Unit,
    onSwitchToLogin: () -> Unit
) {
    AuthForm(
        subtitle = "创建账号，跨设备同步学习数据",
        submitLabel = "注册",
        switchLabel = "已有账号？返回登录",
        username = username,
        onUsernameChange = onUsernameChange,
        password = password,
        onPasswordChange = onPasswordChange,
        error = error,
        submitting = submitting,
        onSubmit = onSubmit,
        onSwitch = onSwitchToLogin
    )
}

/** 登录/注册共用表单：错误文案来自服务端错误码映射，绝不回显内部细节。 */
@Composable
private fun AuthForm(
    subtitle: String,
    submitLabel: String,
    switchLabel: String,
    username: String,
    onUsernameChange: (String) -> Unit,
    password: String,
    onPasswordChange: (String) -> Unit,
    error: String?,
    submitting: Boolean,
    onSubmit: () -> Unit,
    onSwitch: () -> Unit
) {
    val canSubmit = username.isNotBlank() && password.isNotBlank() && !submitting
    Surface(Modifier.fillMaxSize()) {
        Column(
            modifier = Modifier
                .fillMaxSize()
                .statusBarsPadding()
                .navigationBarsPadding()
                .imePadding()
                .verticalScroll(rememberScrollState())
                .padding(horizontal = 24.dp),
            horizontalAlignment = Alignment.CenterHorizontally
        ) {
            Spacer(Modifier.height(72.dp))
            Text("秋招闪卡", style = MaterialTheme.typography.headlineMedium)
            Spacer(Modifier.height(12.dp))
            Text(
                subtitle,
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
            Spacer(Modifier.height(40.dp))
            OutlinedTextField(
                value = username,
                onValueChange = onUsernameChange,
                label = { Text("用户名") },
                singleLine = true,
                enabled = !submitting,
                modifier = Modifier.fillMaxWidth()
            )
            Spacer(Modifier.height(16.dp))
            OutlinedTextField(
                value = password,
                onValueChange = onPasswordChange,
                label = { Text("密码") },
                singleLine = true,
                enabled = !submitting,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password, imeAction = ImeAction.Done),
                keyboardActions = KeyboardActions(onDone = { if (canSubmit) onSubmit() }),
                visualTransformation = PasswordVisualTransformation(),
                modifier = Modifier.fillMaxWidth()
            )
            Spacer(Modifier.height(4.dp))
            // 固定高度的错误槽，避免错误文案出现/消失时表单跳动。
            Box(Modifier.fillMaxWidth().heightIn(min = 20.dp), contentAlignment = Alignment.CenterStart) {
                error?.let {
                    Text(it, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.error)
                }
            }
            Spacer(Modifier.height(12.dp))
            Button(
                onClick = onSubmit,
                enabled = canSubmit,
                modifier = Modifier.fillMaxWidth().height(48.dp)
            ) {
                if (submitting) {
                    CircularProgressIndicator(
                        modifier = Modifier.size(20.dp),
                        strokeWidth = 2.dp,
                        color = LocalContentColor.current
                    )
                } else {
                    Text(submitLabel)
                }
            }
            Spacer(Modifier.height(8.dp))
            TextButton(onClick = onSwitch, enabled = !submitting) { Text(switchLabel) }
            Spacer(Modifier.height(48.dp))
        }
    }
}
