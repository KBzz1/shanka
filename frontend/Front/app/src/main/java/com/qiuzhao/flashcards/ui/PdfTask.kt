package com.qiuzhao.flashcards.ui

import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.LocalContentColor
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.zIndex

/** Task status page keeps the upstream composition but exposes V2.5 background/retry/abandon only. */
@Composable
internal fun PdfTaskScreen(
    state: PdfTaskState,
    generatedCardCount: Int,
    onLeave: () -> Unit,
    onViewDeck: () -> Unit,
    onRetry: () -> Unit,
    onAbandon: () -> Unit,
    errorCode: String? = null,
    onOpenSettings: (() -> Unit)? = null,
) {
    val scale = (LocalConfiguration.current.screenWidthDp / 402f).coerceIn(.75f, 1f)
    Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
        Box(Modifier.fillMaxSize()) {
            when (state) {
                PdfTaskState.GENERATING -> TaskGenerationCard(
                    designScale = scale,
                    modifier = Modifier.padding(start = (16 * scale).dp, top = (132 * scale).dp, end = (16 * scale).dp),
                )
                PdfTaskState.COMPLETE -> TaskCompletedCard(
                    generatedCardCount = generatedCardCount,
                    designScale = scale,
                    modifier = Modifier.padding(start = (16 * scale).dp, top = (132 * scale).dp, end = (16 * scale).dp),
                )
                PdfTaskState.FAILED -> {
                    val block = failedTaskBlock(errorCode)
                    TaskTerminalCard(
                        title = block.title,
                        detail = block.detail,
                        icon = "error",
                        designScale = scale,
                        modifier = Modifier.padding(start = (16 * scale).dp, top = (132 * scale).dp, end = (16 * scale).dp),
                        actionLabel = if (block.canOpenSettings && onOpenSettings != null) "去设置更新 API Key" else null,
                        onAction = onOpenSettings,
                    )
                }
                PdfTaskState.ABANDONED -> TaskTerminalCard(
                    title = "任务已放弃",
                    detail = "这次生成不会继续；你可以从项目中重新创建任务。",
                    icon = "cancel",
                    designScale = scale,
                    modifier = Modifier.padding(start = (16 * scale).dp, top = (132 * scale).dp, end = (16 * scale).dp),
                )
            }
            DeckDetailHeader("生成任务", scale, onLeave, modifier = Modifier.zIndex(1f))
            BottomContentFade(scale, Modifier.align(Alignment.BottomCenter))
            Row(
                Modifier.align(Alignment.BottomCenter).navigationBarsPadding()
                    .padding(start = (16 * scale).dp, end = (16 * scale).dp, bottom = (32 * scale).dp)
                    .fillMaxWidth().height((60 * scale).dp).zIndex(1f),
                horizontalArrangement = Arrangement.spacedBy((12 * scale).dp),
            ) {
                when (state) {
                    PdfTaskState.GENERATING -> {
                        CardListActionButton("安全离开", "arrow_back", false, Modifier.weight(1f), scale, onClick = onLeave)
                        CardListActionButton("放弃任务", "cancel", true, Modifier.weight(1f), scale, onClick = onAbandon)
                    }
                    PdfTaskState.COMPLETE -> CardListActionButton("查看牌组", "style", true, Modifier.fillMaxWidth(), scale, onClick = onViewDeck)
                    PdfTaskState.FAILED -> {
                        CardListActionButton("放弃", "cancel", false, Modifier.weight(1f), scale, onClick = onAbandon)
                        CardListActionButton("重试", "refresh", true, Modifier.weight(1f), scale, onClick = onRetry)
                    }
                    PdfTaskState.ABANDONED -> CardListActionButton("返回项目", "arrow_back", true, Modifier.fillMaxWidth(), scale, onClick = onLeave)
                }
            }
        }
    }
}

@Composable
private fun TaskGenerationCard(designScale: Float, modifier: Modifier = Modifier) = Surface(
    shape = RoundedCornerShape((AppShapeRadius * designScale).dp),
    color = AppColors.Blue.background,
    modifier = modifier.fillMaxWidth().height((265 * designScale).dp),
) {
    Column(
        modifier = Modifier.fillMaxSize().padding((24 * designScale).dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy((20 * designScale).dp),
    ) {
        Md3ExpressiveIndeterminateRing(designScale)
        Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy((8 * designScale).dp)) {
            AppText("正在生成闪卡", AppTextRole.PageTitle, color = PageForegroundColor(), designScale = designScale)
            AppText(
                "可以安全离开此页面，任务会在后台继续；完成后会原子发布。",
                AppTextRole.CardSubtitle,
                color = AppColors.TextIconDark.copy(alpha = .55f),
                designScale = designScale,
                textAlign = TextAlign.Center,
            )
        }
    }
}

@Composable
private fun TaskCompletedCard(generatedCardCount: Int, designScale: Float, modifier: Modifier = Modifier) = Surface(
    shape = RoundedCornerShape((AppShapeRadius * designScale).dp),
    color = AppColors.Blue.background,
    modifier = modifier.fillMaxWidth(),
) {
    Column(
        modifier = Modifier.fillMaxWidth().padding((24 * designScale).dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy((20 * designScale).dp),
    ) {
        MaterialSymbol("check_circle", null, tint = AppColors.Green.primaryStrong, size = fixedSp(80 * designScale), filled = true)
        Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy((8 * designScale).dp)) {
            AppText("卡片组生成完成", AppTextRole.PageTitle, color = AppColors.TextIconDark, designScale = designScale)
            AppText("共生成 ${generatedCardCount.coerceAtLeast(0)} 张闪卡", AppTextRole.CardSubtitle, color = AppColors.TextIconDark.copy(alpha = .55f), designScale = designScale)
        }
    }
}

@Composable
private fun TaskTerminalCard(
    title: String,
    detail: String,
    icon: String,
    designScale: Float,
    modifier: Modifier = Modifier,
    actionLabel: String? = null,
    onAction: (() -> Unit)? = null,
) = Surface(
    shape = RoundedCornerShape((AppShapeRadius * designScale).dp),
    color = AppColors.Blue.background,
    modifier = modifier.fillMaxWidth(),
) {
    Column(
        modifier = Modifier.fillMaxWidth().padding((24 * designScale).dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy((16 * designScale).dp),
    ) {
        MaterialSymbol(icon, null, tint = AppColors.WarningStrong, size = fixedSp(72 * designScale), filled = true)
        AppText(title, AppTextRole.PageTitle, color = AppColors.TextIconDark, designScale = designScale)
        AppText(detail, AppTextRole.CardSubtitle, color = AppColors.TextIconDark.copy(alpha = .65f), designScale = designScale, textAlign = TextAlign.Center)
        if (actionLabel != null && onAction != null) {
            CardListActionButton(actionLabel, "settings", false, Modifier.fillMaxWidth(), designScale, onClick = onAction)
        }
    }
}

private data class TaskFailureBlock(val title: String, val detail: String, val canOpenSettings: Boolean = false)

private fun failedTaskBlock(code: String?): TaskFailureBlock = when (code) {
    "IMPORT_FAILED" -> TaskFailureBlock(
        "导入失败",
        "卡片草稿已保留；重试只会补上失败的步骤，不会重复创建卡组或卡片。",
    )
    "API_KEY_UNAVAILABLE", "API_KEY_INVALID" -> TaskFailureBlock(
        "API Key 不可用",
        "当前保存的 DeepSeek API Key 无效或不可用，请更新有效 Key 后再重试。",
        canOpenSettings = true,
    )
    "API_KEY_NOT_SET" -> TaskFailureBlock(
        "未配置 API Key",
        "请先到设置保存 DeepSeek API Key 后再重试。",
        canOpenSettings = true,
    )
    else -> TaskFailureBlock("生成失败", "原有卡片不会部分发布。你可以重试，或主动放弃这次任务。")
}

@Composable
private fun Md3ExpressiveIndeterminateRing(designScale: Float) {
    val transition = rememberInfiniteTransition(label = "generation ring")
    val rotation by transition.animateFloat(
        initialValue = 0f,
        targetValue = 360f,
        animationSpec = infiniteRepeatable(tween(durationMillis = 1400, easing = LinearEasing)),
        label = "generation ring rotation",
    )
    Canvas(Modifier.size((80 * designScale).dp)) {
        val stroke = with(this) { 8.dp.toPx() }
        val inset = stroke / 2f
        val bounds = androidx.compose.ui.geometry.Rect(inset, inset, size.width - inset, size.height - inset)
        drawArc(AppColors.Blue.primarySecondary, rotation - 220f, 220f, false, bounds.topLeft, bounds.size, style = Stroke(stroke, cap = StrokeCap.Round))
        drawArc(AppColors.Blue.primary, rotation + 50f, 44f, false, bounds.topLeft, bounds.size, style = Stroke(stroke, cap = StrokeCap.Round))
    }
}
