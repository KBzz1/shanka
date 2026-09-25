package com.qiuzhao.flashcards.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
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
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.unit.dp
import androidx.compose.ui.zIndex
import com.qiuzhao.flashcards.data.remote.ProjectSummary
import com.qiuzhao.flashcards.domain.v25.V25ProjectStatus
import com.qiuzhao.flashcards.domain.v25.V25SourceMode
import com.qiuzhao.flashcards.ui.auth.ErrorMessages
import com.qiuzhao.flashcards.ui.navigation.AppRoute

/**
 * V25-D-43 「问答直通」章节选择（非基础路由，从选资料/选章节页右上角进入）。
 *
 * 适用资料本身已包含问题和答案（题库/问答集）：任务以 source_mode=QA_DIRECT 创建，
 * 规划阶段只提取资料已有问答对，生成阶段仅做格式规范化——AI 不重新命题、不改答案。
 * 后续链路复用智能制卡全流程（样卡等待 → 预览 → 生成 → 评审），模式随任务走。
 */
@Composable
internal fun QaCardChapterScreen(project: ProjectSummary, nav: ScreenNavigator, viewModel: AppViewModel) {
    val scale = (LocalConfiguration.current.screenWidthDp / 402f).coerceIn(.75f, 1f)
    val theme = deckTheme(project)
    val activeProject by viewModel.activePdfProject.collectAsState()
    val generationDraft by viewModel.projectGenerationDraft.collectAsState()
    var selectedIds by remember { mutableStateOf(setOf<String>()) }
    var requestError by remember { mutableStateOf<String?>(null) }
    var preparing by remember { mutableStateOf(false) }
    val active = activeProject?.takeIf { it.projectId == project.id }
    val sections = active?.chapters.orEmpty().map { chapter ->
        SmartChapter(chapter.id, chapter.name, chapter.pageSpanLabel ?: "全文", chapter.isAiPlanned)
    }
    val parsing = active?.status == V25ProjectStatus.PARSING
    val blocked = parsing || active?.status == V25ProjectStatus.PARSE_FAILED

    LaunchedEffect(project.id) {
        viewModel.openProjectForGeneration(project.id) { }
    }
    LaunchedEffect(sections) {
        if (selectedIds.isEmpty() && sections.isNotEmpty()) selectedIds = sections.map { it.id }.toSet()
    }

    Box(Modifier.fillMaxSize().background(AppColors.BaseBackground)) {
        ScreenTopInformationBar(
            title = "问答直通", subtitle = null, onBack = nav::goBack,
            backContainer = theme.cardPanel, titleColor = theme.text
        )
        LazyColumn(
            modifier = Modifier.fillMaxSize().statusBarsPadding()
                .padding(start = (16 * scale).dp, top = (88 * scale).dp, end = (16 * scale).dp)
                .clip(RoundedCornerShape((AppScrollableContentClipRadius * scale).dp)),
            contentPadding = PaddingValues(bottom = (fixedBottomControlScrollTail(bottomOffset = 16, controlCount = 2, gapBetweenControls = 12) * scale).dp),
            verticalArrangement = Arrangement.spacedBy((16 * scale).dp)
        ) {
            item {
                Surface(
                    color = theme.cardPanel,
                    shape = RoundedCornerShape((AppNestedShapeRadius * scale).dp),
                    modifier = Modifier.fillMaxWidth()
                ) {
                    AppText(
                        "资料已包含问题和答案：AI 不重新命题、不改答案，只把已有问答整理成闪卡。选择要处理的章节。",
                        AppTextRole.Supporting,
                        modifier = Modifier.fillMaxWidth().padding((24 * scale).dp),
                        color = AppColors.TextIconDark,
                        designScale = scale,
                    )
                }
            }
            requestError?.let { error ->
                item {
                    CardHint(
                        "无法开始问答直通：${ErrorMessages.forCode(error)}",
                        designScale = scale,
                        error = true,
                    )
                }
            }
            if (parsing) {
                item {
                    Box(Modifier.fillMaxWidth(), contentAlignment = Alignment.Center) {
                        StatusProgressCard(
                            title = "正在识别文件内容",
                            subtitle = "已识别文件",
                            container = theme.background,
                            ringColor = theme.primary,
                        )
                    }
                }
            }
            item {
                AppText("部分", AppTextRole.SectionTitle, modifier = Modifier.padding(start = (8 * scale).dp), color = theme.text, designScale = scale)
            }
            items(sections, key = { it.id }) { section ->
                SmartChapterCard(section, selected = section.id in selectedIds, theme, scale) {
                    selectedIds = if (it in selectedIds) selectedIds - it else selectedIds + it
                }
            }
        }
        BottomContentFade(scale, Modifier.align(Alignment.BottomCenter), color = AppColors.BaseBackground)
        Column(
            modifier = Modifier.align(Alignment.BottomCenter).navigationBarsPadding()
                .padding(horizontal = (16 * scale).dp, vertical = (16 * scale).dp)
                .fillMaxWidth().zIndex(1f),
            verticalArrangement = Arrangement.spacedBy((12 * scale).dp)
        ) {
            Surface(
                onClick = {
                    selectedIds = if (selectedIds.size == sections.size) emptySet() else sections.map { it.id }.toSet()
                },
                enabled = sections.isNotEmpty() && !blocked,
                color = theme.secondary, contentColor = AppColors.TextIconDark,
                shape = RoundedCornerShape((24 * scale).dp),
                modifier = Modifier.fillMaxWidth().height((68 * scale).dp)
            ) {
                Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    AppText(
                        if (selectedIds.size == sections.size && sections.isNotEmpty()) "取消全选" else "全选",
                        AppTextRole.Label,
                        color = LocalContentColor.current,
                        designScale = scale,
                        maxLines = 1,
                    )
                }
            }
            Surface(
                onClick = {
                    if (blocked || selectedIds.isEmpty() || preparing) return@Surface
                    preparing = true
                    requestError = null
                    viewModel.beginPdfSamples(
                        existingDeckId = null,
                        deckName = generationDraft?.deckName.orEmpty().ifBlank { "${project.name} 卡片组" },
                        chapterIds = selectedIds.toList(),
                        config = generationDraft?.config ?: PdfGenerationConfig(),
                        onReady = {
                            preparing = false
                            nav.navigate(AppRoute.SmartCardSampleWait(project.id))
                        },
                        onFailure = { code ->
                            preparing = false
                            requestError = code ?: "GENERATION_FAILED"
                        },
                        sourceMode = V25SourceMode.QA_DIRECT,
                    )
                },
                color = theme.primary, contentColor = theme.onPrimary,
                shape = RoundedCornerShape((24 * scale).dp),
                modifier = Modifier.fillMaxWidth().height((68 * scale).dp)
            ) {
                Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    AppText(
                        when {
                            parsing -> "正在解析"
                            preparing -> "正在创建任务"
                            else -> "开始问答直通"
                        },
                        AppTextRole.Label,
                        color = LocalContentColor.current,
                        designScale = scale,
                        maxLines = 1,
                    )
                }
            }
        }
    }
}
