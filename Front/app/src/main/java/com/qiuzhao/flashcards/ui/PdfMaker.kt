package com.qiuzhao.flashcards.ui

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
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListScope
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.LocalContentColor
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.zIndex
import com.qiuzhao.flashcards.data.remote.DeckSummary
import com.qiuzhao.flashcards.data.remote.PdfChapter
import com.qiuzhao.flashcards.domain.v25.V25ProjectStatus
import com.qiuzhao.flashcards.domain.v25.V25TaskStatus
import kotlinx.coroutines.delay

/** V2.5 intentionally has no paused state: generation continues in the background. */
internal enum class PdfTaskState { GENERATING, COMPLETE, FAILED, ABANDONED }

internal data class PdfGenerationBlock(val title: String, val detail: String, val canOpenSettings: Boolean = false)

private enum class ProjectGenerationStep { CHAPTERS, SETTINGS, PREVIEW, TASK }

private data class ChapterChoice(val chapter: PdfChapter, val selected: Boolean = true)

/**
 * Reuses b944790 chapter/settings/sample visuals, but binds them to one existing V2.5 PDF
 * project. Project creation happens on the project page; this flow never accepts text/Markdown
 * and never exposes stopped-task controls.
 */
@Composable
internal fun PdfSmartCardsFlow(decks: List<DeckSummary>, viewModel: AppViewModel, nav: ScreenNavigator) {
    val project by viewModel.activePdfProject.collectAsState()
    val file by viewModel.pdfFile.collectAsState()
    val samples by viewModel.pdfSamples.collectAsState()
    val task by viewModel.pdfTask.collectAsState()
    val taskDeckId by viewModel.pdfTaskDeckId.collectAsState()
    val chapterChoices = remember(project?.projectId) { mutableStateListOf<ChapterChoice>() }
    var step by remember(project?.projectId) { mutableStateOf(ProjectGenerationStep.CHAPTERS) }
    var useExistingDeck by remember(project?.projectId) { mutableStateOf(false) }
    var existingDeckId by remember(project?.projectId) { mutableStateOf<String?>(null) }
    var deckName by remember(project?.projectId) { mutableStateOf("") }
    var coverage by remember(project?.projectId) { mutableStateOf("均匀") }
    var requirement by remember(project?.projectId) { mutableStateOf("") }
    var generationConfig by remember { mutableStateOf(PdfGenerationConfig()) }
    var editingIndex by remember { mutableStateOf<Int?>(null) }
    var message by remember { mutableStateOf<String?>(null) }

    LaunchedEffect(project?.projectId, file?.chapters) {
        val chapters = file?.chapters.orEmpty()
        chapterChoices.clear()
        chapterChoices.addAll(chapters.map { ChapterChoice(it) })
        if (deckName.isBlank()) deckName = project?.name?.let { "$it 卡片组" }.orEmpty()
    }
    LaunchedEffect(project?.projectId, project?.status) {
        if (project?.status == V25ProjectStatus.PARSING) {
            while (true) {
                delay(2_500)
                viewModel.refreshActiveProject()
            }
        }
    }
    LaunchedEffect(step, task?.taskId) {
        if (step == ProjectGenerationStep.TASK && task != null) viewModel.refreshPdfTask()
    }

    val activeProject = project
    when {
        activeProject == null -> PdfFlowLayout("智能制卡", nav::goBack) {
            item { PdfStatusCard("请从项目详情开始", "先选择一个 PDF 学习项目，再确认章节并生成闪卡。", icon = "folder_open") }
        }
        activeProject.status == V25ProjectStatus.PARSING -> PdfFlowLayout("正在识别", nav::goBack) {
            item { PdfStatusCard("正在解析 PDF", "解析完成后会显示章节；现在可以安全离开，稍后从项目中继续。", loading = true) }
        }
        activeProject.status == V25ProjectStatus.PARSE_FAILED -> PdfFlowLayout("PDF 解析失败", nav::goBack) {
            item { PdfStatusCard("PDF 解析失败", "请在项目中替换 PDF 后重试。", icon = "error") }
        }
        else -> when (step) {
            ProjectGenerationStep.CHAPTERS -> ProjectChapterScreen(
                chapters = chapterChoices,
                confirmationRequired = activeProject.status == V25ProjectStatus.AWAITING_CHAPTER_CONFIRMATION,
                onToggle = { index -> chapterChoices[index] = chapterChoices[index].copy(selected = !chapterChoices[index].selected) },
                onEdit = { editingIndex = it },
                onDelete = { index ->
                    val removed = chapterChoices[index]
                    chapterChoices.removeAt(index)
                    viewModel.deletePdfChapter(removed.chapter) {
                        chapterChoices.add(index.coerceIn(0, chapterChoices.size), removed)
                    }
                },
                onNext = {
                    if (chapterChoices.none { it.selected }) {
                        message = "请至少选择一个章节"
                    } else if (activeProject.status == V25ProjectStatus.AWAITING_CHAPTER_CONFIRMATION) {
                        viewModel.confirmPdfChapters { error ->
                            message = error
                            if (error == null) step = ProjectGenerationStep.SETTINGS
                        }
                    } else {
                        step = ProjectGenerationStep.SETTINGS
                    }
                },
                onBack = nav::goBack,
            )
            ProjectGenerationStep.SETTINGS -> PdfSettingsScreen(
                decks = decks.filter { it.projectId == activeProject.projectId },
                useExistingDeck = useExistingDeck,
                onUseExisting = { useExistingDeck = it },
                selectedExistingDeckId = existingDeckId,
                onSelectedExistingDeck = { existingDeckId = it },
                deckName = deckName,
                onDeckNameChange = { deckName = it },
                coverage = coverage,
                onCoverageChange = { coverage = it },
                requirement = requirement,
                onRequirementChange = { requirement = it },
                onPreview = { basic, analysis ->
                    generationConfig = PdfGenerationConfig(
                        quantity = when (coverage) {
                            "精简" -> "COMPACT"
                            "充分" -> "EXTENSIVE"
                            else -> "BALANCED"
                        },
                        basic = basic / 100f,
                        understanding = (analysis - basic) / 100f,
                        application = (100f - analysis) / 100f,
                        requirement = requirement,
                    )
                    val chapterIds = chapterChoices.filter { it.selected }.map { it.chapter.id }
                    viewModel.checkApiKeyForGeneration(
                        onAvailable = {
                            viewModel.generatePdfSamples(
                                existingDeckId = if (useExistingDeck) existingDeckId else null,
                                deckName = deckName,
                                chapterIds = chapterIds,
                                config = generationConfig,
                                onReady = { step = ProjectGenerationStep.PREVIEW },
                                onFailure = { code -> message = sampleGenerationBlock(code).detail },
                            )
                        },
                        onUnavailable = { status -> message = apiKeyGenerationBlock(status).detail },
                        onFailure = { message = "无法确认 API Key，请检查网络后重试。" },
                    )
                },
                onBack = { step = ProjectGenerationStep.CHAPTERS },
            )
            ProjectGenerationStep.PREVIEW -> PdfPreviewScreen(
                samples = samples,
                onBack = { step = ProjectGenerationStep.SETTINGS },
                onGenerate = {
                    viewModel.startPdfTask(
                        onStarted = { step = ProjectGenerationStep.TASK },
                        onFailure = { code -> message = taskGenerationBlock(code).detail },
                    )
                },
            )
            ProjectGenerationStep.TASK -> PdfTaskScreen(
                state = task.toPdfTaskState(),
                generatedCardCount = task?.generatedCardCount ?: 0,
                onLeave = nav::goBack,
                onViewDeck = {
                    taskDeckId?.let { nav.replaceTop(com.qiuzhao.flashcards.ui.navigation.AppRoute.CardList(it)) }
                },
                onRetry = {
                    viewModel.retryPdfTask { step = ProjectGenerationStep.PREVIEW }
                },
                onAbandon = { viewModel.abandonPdfTask(nav::goBack) },
            )
        }
    }

    editingIndex?.let { index ->
        val choice = chapterChoices.getOrNull(index) ?: return@let
        PdfChapterEditDialog(
            chapter = choice.chapter,
            onSave = { updated ->
                chapterChoices[index] = choice.copy(chapter = updated)
                viewModel.updatePdfChapter(updated) {
                    chapterChoices[index] = choice
                }
                editingIndex = null
            },
            onDismiss = { editingIndex = null },
        )
    }
    message?.let { text ->
        PdfGenerationBlockedDialog(
            block = PdfGenerationBlock("无法继续", text),
            onDismiss = { message = null },
            onOpenSettings = { message = null; nav.navigate(com.qiuzhao.flashcards.ui.navigation.AppRoute.Settings) },
        )
    }
}

private fun com.qiuzhao.flashcards.domain.v25.V25GenerationTask?.toPdfTaskState(): PdfTaskState = when (this?.status) {
    V25TaskStatus.COMPLETED -> PdfTaskState.COMPLETE
    V25TaskStatus.FAILED -> PdfTaskState.FAILED
    V25TaskStatus.ABANDONED -> PdfTaskState.ABANDONED
    else -> PdfTaskState.GENERATING
}

private fun apiKeyGenerationBlock(status: String): PdfGenerationBlock = when (status.uppercase()) {
    "INVALID" -> PdfGenerationBlock("API Key 不可用", "请在设置中更新有效的 DeepSeek API Key。", canOpenSettings = true)
    "INSUFFICIENT_BALANCE" -> PdfGenerationBlock("API Key 余额不足", "请在设置中更新可用的 DeepSeek API Key。", canOpenSettings = true)
    else -> PdfGenerationBlock("需要 API Key", "请先在设置中保存可用的 DeepSeek API Key。", canOpenSettings = true)
}

private fun taskGenerationBlock(code: String?): PdfGenerationBlock = when (code) {
    "SAMPLE_STALE" -> PdfGenerationBlock("样卡已失效", "请重新生成样卡并确认后开始。")
    else -> PdfGenerationBlock("暂时无法开始生成", "服务暂时无法创建任务，请稍后重试。")
}

private fun sampleGenerationBlock(code: String?): PdfGenerationBlock = when (code) {
    "CHAPTER_NOT_FOUND" -> PdfGenerationBlock("未选择章节", "请返回上一步选择至少一个章节。")
    "TASK_STATE_CONFLICT" -> PdfGenerationBlock("任务状态已变化", "请刷新项目后重试。")
    else -> PdfGenerationBlock("暂时无法生成样卡", "服务暂时无法生成样卡，请稍后重试。")
}

@Composable
internal fun PdfFlowLayout(
    title: String,
    onBack: () -> Unit,
    footer: @Composable (() -> Unit)? = null,
    content: LazyListScope.() -> Unit,
) {
    val scale = (LocalConfiguration.current.screenWidthDp / 402f).coerceIn(.75f, 1f)
    Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
        Box(Modifier.fillMaxSize()) {
            LazyColumn(
                modifier = Modifier.fillMaxSize().padding(start = (16 * scale).dp, top = (132 * scale).dp, end = (16 * scale).dp)
                    .clip(RoundedCornerShape((AppScrollableContentClipRadius * scale).dp)),
                contentPadding = PaddingValues(bottom = if (footer == null) (NaturalScrollTail * scale).dp else (fixedBottomControlScrollTail(bottomOffset = 24) * scale).dp),
                verticalArrangement = Arrangement.spacedBy((16 * scale).dp),
                content = content,
            )
            DeckDetailHeader(title, scale, onBack, modifier = Modifier.zIndex(1f))
            if (footer != null) {
                BottomContentFade(scale, Modifier.align(Alignment.BottomCenter))
                Box(
                    Modifier.align(Alignment.BottomCenter).navigationBarsPadding()
                        .padding(start = (16 * scale).dp, end = (16 * scale).dp, bottom = (24 * scale).dp).zIndex(1f),
                ) { footer() }
            }
        }
    }
}

@Composable
private fun PdfStatusCard(title: String, subtitle: String, loading: Boolean = false, icon: String = "picture_as_pdf") = Card(
    shape = RoundedCornerShape(AppShapeRadius.dp),
    colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant),
    modifier = Modifier.fillMaxWidth(),
) {
    Row(
        Modifier.padding(24.dp),
        horizontalArrangement = Arrangement.spacedBy(16.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        if (loading) CircularProgressIndicator(modifier = Modifier.size(40.dp), strokeWidth = 4.dp)
        else MaterialSymbol(icon, null, tint = MaterialTheme.colorScheme.error, size = fixedSp(40f), filled = true)
        Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
            MixedLanguageText(
                text = title,
                color = PageForegroundColor(),
                chineseFont = AppFonts.MiSansSemibold,
                latinFont = AppFonts.GoogleSansFlexSemibold,
                fontSize = fixedSp(19f),
                lineHeight = fixedSp(24f),
            )
            MixedLanguageText(
                text = subtitle,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                chineseFont = AppFonts.MiSansMedium,
                latinFont = AppFonts.GoogleSansFlex,
                fontSize = fixedSp(14f),
                lineHeight = fixedSp(18f),
            )
        }
    }
}

@Composable
private fun ProjectChapterScreen(
    chapters: List<ChapterChoice>,
    confirmationRequired: Boolean,
    onToggle: (Int) -> Unit,
    onEdit: (Int) -> Unit,
    onDelete: (Int) -> Unit,
    onNext: () -> Unit,
    onBack: () -> Unit,
) {
    val scale = (LocalConfiguration.current.screenWidthDp / 402f).coerceIn(.75f, 1f)
    PdfFlowLayout(
        title = "确认章节",
        onBack = onBack,
        footer = {
            DetailPrimaryButton(
                if (confirmationRequired) "确认章节" else "生成设置",
                "arrow_forward",
                true,
                scale,
                onNext,
            )
        },
    ) {
        item {
            PdfStatusCard(
                "${if (confirmationRequired) "确认" else "选择"}要生成闪卡的章节",
                if (confirmationRequired) "确认后会冻结本次生成使用的章节范围。" else "选择章节后可继续创建样卡。",
                icon = "menu_book",
            )
        }
        if (chapters.isEmpty()) item { PdfStatusCard("暂未识别到章节", "请返回项目稍后刷新，或替换可解析的 PDF。", icon = "error") }
        items(chapters.indices.toList()) { index ->
            val choice = chapters[index]
            ChapterChoiceCard(choice, scale, { onToggle(index) }, { onEdit(index) }, { onDelete(index) })
        }
    }
}

@Composable
private fun ChapterChoiceCard(
    choice: ChapterChoice,
    scale: Float,
    onToggle: () -> Unit,
    onEdit: () -> Unit,
    onDelete: () -> Unit,
) {
    val surface = if (choice.selected) AppColors.Green.background else AppColors.Blue.background
    val accent = if (choice.selected) AppColors.Green.primary else AppColors.Blue.primary
    Surface(onClick = onToggle, color = surface, shape = RoundedCornerShape((AppShapeRadius * scale).dp), modifier = Modifier.fillMaxWidth()) {
        Row(
            Modifier.padding((20 * scale).dp),
            horizontalArrangement = Arrangement.spacedBy((12 * scale).dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Surface(color = accent, shape = RoundedCornerShape((16 * scale).dp), modifier = Modifier.size((48 * scale).dp)) {
                Box(contentAlignment = Alignment.Center) {
                    MaterialSymbol(if (choice.selected) "check_circle" else "book_ribbon", null, tint = AppColors.TextIconLight, size = fixedSp(24 * scale), filled = true)
                }
            }
            Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy((4 * scale).dp)) {
                AppText(choice.chapter.name, AppTextRole.CardTitle, color = AppColors.TextIconDark, designScale = scale, maxLines = 1, overflow = TextOverflow.Ellipsis)
                AppText("${choice.chapter.startPage}-${choice.chapter.endPage} 页", AppTextRole.CardSubtitle, color = AppColors.TextIconDark.copy(alpha = .65f), designScale = scale)
            }
            TextButton(onClick = onEdit) { Text("编辑") }
            TextButton(onClick = onDelete) { Text("删除", color = AppColors.WarningStrong) }
        }
    }
}

@Composable
private fun PdfChapterEditDialog(chapter: PdfChapter, onSave: (PdfChapter) -> Unit, onDismiss: () -> Unit) {
    var title by remember(chapter) { mutableStateOf(chapter.name) }
    var start by remember(chapter) { mutableStateOf(chapter.startPage.toString()) }
    var end by remember(chapter) { mutableStateOf(chapter.endPage.toString()) }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("编辑章节", fontFamily = AppFonts.MiSansSemibold, fontWeight = FontWeight.Normal) },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                OutlinedTextField(title, { title = it }, label = { Text("章节名称") }, modifier = Modifier.fillMaxWidth())
                Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                    OutlinedTextField(start, { start = it.filter(Char::isDigit) }, label = { Text("起始页") }, modifier = Modifier.weight(1f))
                    OutlinedTextField(end, { end = it.filter(Char::isDigit) }, label = { Text("结束页") }, modifier = Modifier.weight(1f))
                }
            }
        },
        confirmButton = {
            TextButton(onClick = {
                onSave(
                    chapter.copy(
                        name = title.trim().ifBlank { chapter.name },
                        startPage = start.toIntOrNull() ?: chapter.startPage,
                        endPage = end.toIntOrNull() ?: chapter.endPage,
                    ),
                )
            }) { Text("保存") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("取消") } },
    )
}
