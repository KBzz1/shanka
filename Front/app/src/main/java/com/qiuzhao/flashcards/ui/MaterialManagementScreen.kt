package com.qiuzhao.flashcards.ui

import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.LocalContentColor
import androidx.compose.material3.Surface
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.zIndex
import com.qiuzhao.flashcards.data.remote.ProjectSummary
import com.qiuzhao.flashcards.domain.v25.V25LearningProject
import com.qiuzhao.flashcards.domain.v25.V25ProjectStatus
import com.qiuzhao.flashcards.ui.auth.ErrorMessages
import kotlinx.coroutines.delay

/**
 * Project-scoped PDF management. V2.5 deliberately owns exactly one PDF per project: this
 * screen exposes its server state and the replacement action for a failed parse, without
 * presenting the upstream text/multi-file draft controls that have no Release contract.
 */
@Composable
internal fun MaterialManagementScreen(project: ProjectSummary, viewModel: AppViewModel, nav: ScreenNavigator) {
    val scale = (LocalConfiguration.current.screenWidthDp / 402f).coerceIn(.75f, 1f)
    val theme = deckTheme(project)
    val activeProject by viewModel.activePdfProject.collectAsState()
    var replacing by remember(project.id) { mutableStateOf(false) }
    var actionMessage by remember(project.id) { mutableStateOf<String?>(null) }

    LaunchedEffect(project.id) {
        viewModel.openProjectForGeneration(project.id) {}
    }

    // Parsing is an asynchronous backend transition. Poll only while this page is showing the
    // parsing state; changing state cancels this effect and prevents background work after exit.
    LaunchedEffect(activeProject?.projectId, activeProject?.status) {
        if (activeProject?.projectId == project.id && activeProject?.status == V25ProjectStatus.PARSING) {
            while (true) {
                delay(2_000)
                viewModel.refreshActiveProject()
            }
        }
    }

    val picker = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        if (uri == null) return@rememberLauncherForActivityResult
        replacing = true
        actionMessage = null
        viewModel.replaceProjectPdf(project.id, uri) { success, message ->
            replacing = false
            actionMessage = if (success) "PDF 已提交，正在重新解析" else message ?: ErrorMessages.UNKNOWN_ERROR_MESSAGE
        }
    }

    Box(Modifier.fillMaxSize().background(AppColors.BaseBackground)) {
        ScreenTopInformationBar(
            title = "资料管理",
            subtitle = null,
            onBack = nav::goBack,
            backContainer = theme.cardPanel,
            titleColor = theme.text,
        )
        LazyColumn(
            modifier = Modifier.fillMaxSize().statusBarsPadding()
                .padding(start = (16 * scale).dp, top = (88 * scale).dp, end = (16 * scale).dp),
            contentPadding = PaddingValues(bottom = (fixedBottomControlScrollTail(bottomOffset = 16) * scale).dp),
            verticalArrangement = Arrangement.spacedBy((16 * scale).dp),
        ) {
            item {
                Surface(
                    color = theme.cardPanel,
                    shape = RoundedCornerShape((24 * scale).dp),
                    modifier = Modifier.fillMaxWidth().height((72 * scale).dp),
                ) {
                    Box(Modifier.padding(horizontal = (24 * scale).dp), contentAlignment = Alignment.CenterStart) {
                        AppText(
                            "每个学习项目包含一份 PDF。解析失败时可替换文件，成功后请在项目中确认章节。",
                            AppTextRole.Supporting,
                            color = theme.text,
                            designScale = scale,
                            maxLines = 2,
                            overflow = TextOverflow.Ellipsis,
                        )
                    }
                }
            }
            item {
                when {
                    activeProject == null ->
                        Surface(
                            color = theme.surface,
                            shape = RoundedCornerShape((24 * scale).dp),
                            modifier = Modifier.fillMaxWidth().height((156 * scale).dp),
                        ) {
                            Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                                CircularProgressIndicator(color = theme.primary)
                            }
                        }
                    activeProject?.projectId != project.id -> Unit
                    else -> PdfMaterialCard(activeProject!!, theme, scale)
                }
            }
            actionMessage?.let { message ->
                item {
                    Surface(
                        color = theme.cardPanel,
                        shape = RoundedCornerShape((20 * scale).dp),
                        modifier = Modifier.fillMaxWidth(),
                    ) {
                        AppText(message, AppTextRole.Supporting, color = theme.text, designScale = scale, modifier = Modifier.padding((20 * scale).dp))
                    }
                }
            }
        }
        BottomContentFade(scale, Modifier.align(Alignment.BottomCenter), color = AppColors.BaseBackground)
        val status = activeProject?.takeIf { it.projectId == project.id }?.status
        val canReplace = status == V25ProjectStatus.PARSE_FAILED && !replacing
        Surface(
            onClick = { if (canReplace) picker.launch(arrayOf("application/pdf")) },
            color = if (canReplace) theme.primary else theme.cardPanel.copy(alpha = .72f),
            contentColor = if (canReplace) theme.onPrimary else theme.text.copy(alpha = .55f),
            shape = RoundedCornerShape((24 * scale).dp),
            modifier = Modifier.align(Alignment.BottomCenter).navigationBarsPadding()
                .padding(horizontal = (16 * scale).dp, vertical = (16 * scale).dp)
                .fillMaxWidth().height((60 * scale).dp).zIndex(1f),
        ) {
            Row(Modifier.fillMaxSize(), horizontalArrangement = Arrangement.Center, verticalAlignment = Alignment.CenterVertically) {
                if (replacing || status == V25ProjectStatus.PARSING) {
                    CircularProgressIndicator(modifier = Modifier.height((20 * scale).dp), color = LocalContentColor.current, strokeWidth = 2.dp)
                    Spacer(Modifier.width((8 * scale).dp))
                } else {
                    MaterialSymbol("folder_open", null, tint = LocalContentColor.current, size = fixedSp(24 * scale), filled = true)
                    Spacer(Modifier.width((8 * scale).dp))
                }
                AppText(
                    when {
                        replacing -> "正在上传 PDF"
                        status == V25ProjectStatus.PARSING -> "正在解析 PDF"
                        status == V25ProjectStatus.PARSE_FAILED -> "替换 PDF"
                        else -> "当前项目仅支持一份 PDF"
                    },
                    AppTextRole.Label,
                    color = LocalContentColor.current,
                    designScale = scale,
                )
            }
        }
    }
}

@Composable
private fun PdfMaterialCard(project: V25LearningProject, theme: DeckTheme, scale: Float) {
    val statusColor = when (project.status) {
        V25ProjectStatus.PARSE_FAILED -> AppColors.Warning
        V25ProjectStatus.PARSING -> AppColors.Warning
        else -> theme.primary
    }
    Surface(
        color = theme.surface,
        shape = RoundedCornerShape((24 * scale).dp),
        modifier = Modifier.fillMaxWidth(),
    ) {
        Column(
            Modifier.fillMaxWidth().padding((24 * scale).dp),
            verticalArrangement = Arrangement.spacedBy((12 * scale).dp),
        ) {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy((12 * scale).dp)) {
                Surface(color = theme.cardPanel, shape = RoundedCornerShape((16 * scale).dp), modifier = Modifier.height((48 * scale).dp).width((48 * scale).dp)) {
                    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                        MaterialSymbol("picture_as_pdf", null, tint = statusColor, size = fixedSp(26 * scale), filled = true)
                    }
                }
                Column(Modifier.weight(1f)) {
                    AppText(project.file.name, AppTextRole.CardTitle, color = theme.text, designScale = scale, maxLines = 1, overflow = TextOverflow.Ellipsis)
                    val metadata = buildList {
                        project.file.sizeBytes?.let { add(formatBytes(it)) }
                        add("${project.file.chapters.size} 个章节")
                    }.joinToString(" · ")
                    AppText(metadata, AppTextRole.CardSubtitle, color = theme.text.copy(alpha = .62f), designScale = scale, maxLines = 1, overflow = TextOverflow.Ellipsis)
                }
            }
            AppText(statusLabel(project.status), AppTextRole.Label, color = statusColor, designScale = scale)
            if (project.status == V25ProjectStatus.PARSE_FAILED) {
                AppText(
                    ErrorMessages.forCode(project.file.errorCode ?: "PDF_PARSE_FAILED"),
                    AppTextRole.Supporting,
                    color = theme.text.copy(alpha = .72f),
                    designScale = scale,
                )
            } else if (project.status == V25ProjectStatus.READY) {
                AppText("${project.deckCount} 个牌组 · ${project.taskCount} 个生成任务", AppTextRole.Supporting, color = theme.text.copy(alpha = .72f), designScale = scale)
            }
        }
    }
}

private fun statusLabel(status: V25ProjectStatus): String = when (status) {
    V25ProjectStatus.PARSING -> "正在解析"
    V25ProjectStatus.PARSE_FAILED -> "解析失败"
    V25ProjectStatus.AWAITING_CHAPTER_CONFIRMATION -> "等待确认章节"
    V25ProjectStatus.READY -> "已就绪"
}

private fun formatBytes(bytes: Long): String = when {
    bytes < 1024L -> "$bytes B"
    bytes < 1024L * 1024L -> "${bytes / 1024L} KB"
    bytes < 1024L * 1024L * 1024L -> "${bytes / (1024L * 1024L)} MB"
    else -> "${bytes / (1024L * 1024L * 1024L)} GB"
}
