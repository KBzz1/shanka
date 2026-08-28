package com.qiuzhao.flashcards.ui

import androidx.compose.animation.core.FastOutSlowInEasing
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.requiredHeight
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.LocalContentColor
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.TextUnit
import androidx.compose.ui.unit.dp
import androidx.compose.ui.zIndex
import com.qiuzhao.flashcards.data.remote.DeckSummary
import com.qiuzhao.flashcards.data.remote.ProjectSummary
import com.qiuzhao.flashcards.domain.v25.V25DeletionPreflight
import com.qiuzhao.flashcards.domain.v25.V25GenerationTask
import com.qiuzhao.flashcards.domain.v25.V25TaskStatus
import com.qiuzhao.flashcards.ui.navigation.AppRoute

/** Figma 540:3778: a project owns a statistics and deck-management view. */
@Composable
internal fun ProjectDetailScreen(
    project: ProjectSummary,
    decks: List<DeckSummary>,
    nav: ScreenNavigator,
    onDeleteDeck: (String) -> Unit,
    onDeleteProject: (retainDecks: Boolean, onResult: (Boolean) -> Unit) -> Unit,
    tasks: List<V25GenerationTask> = emptyList(),
    deletionPreflight: V25DeletionPreflight? = null,
    onDeleteProjectWithAbandon: ((Boolean, Boolean, (Boolean) -> Unit) -> Unit)? = null,
    deckDeletionPreflight: (String) -> V25DeletionPreflight? = { null },
    onRefreshDeckDeletionPreflight: (String) -> Unit = {},
    onDeleteDeckWithAbandon: ((String, Boolean, (Boolean) -> Unit) -> Unit)? = null,
) {
    val scale = (LocalConfiguration.current.screenWidthDp / 402f).coerceIn(.75f, 1f)
    val theme = deckTheme(project)
    var section by rememberSaveable { mutableStateOf(ProjectDetailSection.STATISTICS) }
    var showProjectDeletionConfirmation by rememberSaveable(project.id) { mutableStateOf(false) }
    var projectDeletionInFlight by rememberSaveable(project.id) { mutableStateOf(false) }
    var deckPendingDeletion by rememberSaveable { mutableStateOf<String?>(null) }
    var deckDeletionInFlight by rememberSaveable { mutableStateOf(false) }
    // The coloured project canvas uses the family Background token; every
    // project-owned deck card then lifts to that family's Surface token.
    Box(Modifier.fillMaxSize().background(theme.background)) {
        ScreenTopInformationBar(
            title = project.name, subtitle = null, onBack = nav::goBack,
            backContainer = theme.cardPanel,
            onTrailingAction = { nav.navigate(AppRoute.ProjectEdit(project.id)) },
            trailingActionSymbol = "edit", trailingActionDescription = "编辑项目",
            trailingActionContainer = theme.cardPanel,
            onSecondaryTrailingAction = { showProjectDeletionConfirmation = true },
            secondaryTrailingActionDescription = "删除项目"
        )
        Column(
            modifier = Modifier.fillMaxSize().statusBarsPadding().padding(start = (16 * scale).dp, top = (88 * scale).dp, end = (16 * scale).dp),
            verticalArrangement = Arrangement.spacedBy((16 * scale).dp)
        ) {
            ProjectSectionSwitcher(section, { section = it }, theme = deckTheme(project))
            when (section) {
                ProjectDetailSection.STATISTICS -> ProjectStatisticsContent(decks, tasks, theme, scale, Modifier.weight(1f))
                ProjectDetailSection.DECKS -> ProjectDecksContent(
                    project,
                    decks,
                    scale,
                    nav,
                    onRequestDeleteDeck = {
                        deckPendingDeletion = it
                        onRefreshDeckDeletionPreflight(it)
                    },
                    modifier = Modifier.weight(1f),
                )
            }
        }
        if (section == ProjectDetailSection.DECKS) {
            BottomContentFade(scale, Modifier.align(Alignment.BottomCenter), color = theme.background)
            ProjectDeckActions(
                theme = deckTheme(project),
                scale = scale,
                onAddDeck = { nav.navigate(AppRoute.DeckGeneration(project.id)) },
                modifier = Modifier.align(Alignment.BottomCenter).zIndex(1f)
            )
        }
    }
    if (showProjectDeletionConfirmation) {
        ProjectDeletionDialog(
            projectName = project.name,
            theme = theme,
            deleting = projectDeletionInFlight,
            onConfirm = { retainDecks ->
                if (projectDeletionInFlight) return@ProjectDeletionDialog
                projectDeletionInFlight = true
                onDeleteProject(retainDecks) { succeeded ->
                    projectDeletionInFlight = false
                    if (succeeded) {
                        showProjectDeletionConfirmation = false
                        nav.returnToTopLevel()
                    }
                }
            },
            preflight = deletionPreflight,
            onConfirmWithAbandon = { retainDecks, abandon ->
                if (projectDeletionInFlight) return@ProjectDeletionDialog
                projectDeletionInFlight = true
                val finish: (Boolean) -> Unit = { succeeded ->
                    projectDeletionInFlight = false
                    if (succeeded) {
                        showProjectDeletionConfirmation = false
                        nav.returnToTopLevel()
                    }
                }
                if (onDeleteProjectWithAbandon != null) {
                    onDeleteProjectWithAbandon(retainDecks, abandon, finish)
                } else {
                    onDeleteProject(retainDecks, finish)
                }
            },
            onDismiss = { if (!projectDeletionInFlight) showProjectDeletionConfirmation = false }
        )
    }
    deckPendingDeletion?.let { deckId ->
        val deck = decks.firstOrNull { it.id == deckId }
        if (deck != null) {
            val preflight = deckDeletionPreflight(deckId)
            val canConfirm = if (onDeleteDeckWithAbandon == null) {
                true
            } else {
                preflight != null && !preflight.hasUncancellableTasks &&
                    (preflight.canDelete || preflight.abandonableTaskIds.isNotEmpty())
            }
            AlertDialog(
                onDismissRequest = { if (!deckDeletionInFlight) deckPendingDeletion = null },
                title = { Text("删除卡片组", fontFamily = AppFonts.MiSansSemibold) },
                text = {
                    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                        Text(
                            "“${displayDeckTitle(deck)}”及其中的卡片、复习记录将被删除。此操作不可恢复。",
                            style = MaterialTheme.typography.bodyMedium,
                        )
                        when {
                            preflight == null && onDeleteDeckWithAbandon != null -> Text(
                                "正在检查进行中的制卡任务…",
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                            preflight?.hasUncancellableTasks == true -> Text(
                                "存在正式生成中的任务，请等待任务结束后再删除。",
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.error,
                            )
                            preflight != null && preflight.abandonableTaskIds.isNotEmpty() -> Text(
                                "检测到 ${preflight.abandonableTaskIds.size} 个准备阶段任务；确认后会先标记为已放弃。",
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                            preflight?.canDelete == true -> Text(
                                "当前没有进行中的任务。",
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                        }
                        preflight?.blockers?.take(3)?.forEach { blocker ->
                            Text(
                                "任务 ${blocker.taskId.take(8)}：${taskStatusLabel(blocker.status)}",
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                        }
                    }
                },
                confirmButton = {
                    TextButton(
                        enabled = canConfirm && !deckDeletionInFlight,
                        onClick = {
                            val abandon = preflight?.let {
                                it.blockers.isNotEmpty() && it.blockers.all { blocker -> blocker.canAbandon }
                            } ?: false
                            if (onDeleteDeckWithAbandon != null) {
                                deckDeletionInFlight = true
                                onDeleteDeckWithAbandon(deckId, abandon) { succeeded ->
                                    deckDeletionInFlight = false
                                    if (succeeded) deckPendingDeletion = null
                                }
                            } else {
                                deckPendingDeletion = null
                                onDeleteDeck(deckId)
                            }
                        },
                    ) { Text(if (deckDeletionInFlight) "正在删除" else "确认删除", color = MaterialTheme.colorScheme.error) }
                },
                dismissButton = {
                    TextButton(
                        enabled = !deckDeletionInFlight,
                        onClick = { deckPendingDeletion = null },
                    ) { Text("取消") }
                },
            )
        }
    }
}

@Composable
private fun ProjectStatisticsContent(
    decks: List<DeckSummary>,
    tasks: List<V25GenerationTask>,
    theme: DeckTheme,
    scale: Float,
    modifier: Modifier,
) {
    var showToday by rememberSaveable { mutableStateOf(true) }
    // There is no project-statistics endpoint: the only real numbers are the sums of this
    // project's decks (all served by GET /decks). Every other slot keeps its Figma layout
    // and shows an honest dash instead of a fabricated value.
    val aggregate = projectDeckAggregate(decks)
    LazyColumn(
        modifier = modifier.fillMaxWidth().clip(RoundedCornerShape((AppScrollableContentClipRadius * scale).dp)),
        // Statistics has no fixed bottom action bar. A 32dp tail places the
        // final cards just above the system navigation area, as in Figma.
        contentPadding = PaddingValues(bottom = (NaturalScrollTail * scale).dp),
        verticalArrangement = Arrangement.spacedBy((16 * scale).dp)
    ) {
        item { ProjectTaskStatusCard(tasks, theme, scale) }
        item {
            LearningDataProgressCard(
                // Today's project review data has no server source; keep the layout, show dashes.
                reviewedCards = null,
                totalCards = null,
                progressPercent = null,
                todaySelected = showToday,
                onTodaySelected = { showToday = it },
                theme = theme,
                designScale = scale
            )
        }
        item {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy((16 * scale).dp)) {
                StatisticsMetricCard(
                    // No learning-time source exists; never borrow a different metric.
                    value = "—",
                    kind = StatisticsMetricKind.LearningTime,
                    surface = StatisticsMetricSurface.White,
                    designScale = scale,
                    modifier = Modifier.weight(1f)
                )
                StatisticsMetricCard(
                    value = honestCount(aggregate.masteredCount),
                    kind = StatisticsMetricKind.MasteredCards,
                    surface = StatisticsMetricSurface.White,
                    designScale = scale,
                    modifier = Modifier.weight(1f)
                )
            }
        }
        item { ProjectProgressDistribution(scale) }
        item { ProjectStreakMetrics(scale) }
    }
}

@Composable
private fun ProjectTaskStatusCard(
    tasks: List<V25GenerationTask>,
    theme: DeckTheme,
    scale: Float,
) {
    val active = tasks.count {
        it.status == V25TaskStatus.DRAFT ||
            it.status == V25TaskStatus.SAMPLE_GENERATING ||
            it.status == V25TaskStatus.AWAITING_SAMPLE_CONFIRMATION ||
            it.status == V25TaskStatus.GENERATING
    }
    Surface(
        color = theme.cardPanel,
        contentColor = theme.text,
        shape = RoundedCornerShape((AppNestedShapeRadius * scale).dp),
        modifier = Modifier.fillMaxWidth(),
    ) {
        Column(
            modifier = Modifier.padding((20 * scale).dp),
            verticalArrangement = Arrangement.spacedBy((12 * scale).dp),
        ) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Row(horizontalArrangement = Arrangement.spacedBy((8 * scale).dp), verticalAlignment = Alignment.CenterVertically) {
                    MaterialSymbol("sync", null, tint = theme.primary, size = fixedSp(20 * scale), filled = true)
                    AppText("制卡任务", AppTextRole.CardTitle, color = theme.text, designScale = scale)
                }
                AppText(
                    if (active == 0) "无进行中任务" else "$active 个进行中",
                    AppTextRole.CardSubtitle,
                    color = if (active == 0) theme.text.copy(alpha = .65f) else theme.primary,
                    designScale = scale,
                )
            }
            if (tasks.isEmpty()) {
                AppText("暂无任务。进入项目后，任务状态会在这里持续更新。", AppTextRole.CardSubtitle, color = theme.text.copy(alpha = .7f), designScale = scale)
            } else {
                tasks.take(5).forEach { task ->
                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.SpaceBetween,
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy((2 * scale).dp)) {
                            AppText("任务 ${task.taskId.take(8)}", AppTextRole.CardSubtitle, color = theme.text, designScale = scale, maxLines = 1)
                            AppText(taskStatusLabel(task.status), AppTextRole.CardSubtitle, color = theme.text.copy(alpha = .65f), designScale = scale, maxLines = 1)
                        }
                        Surface(
                            color = taskStatusColor(task.status, theme),
                            shape = RoundedCornerShape(999.dp),
                        ) {
                            AppText(task.status.name, AppTextRole.Label, color = theme.text, designScale = scale, modifier = Modifier.padding(horizontal = (8 * scale).dp, vertical = (4 * scale).dp), maxLines = 1)
                        }
                    }
                }
                if (tasks.size > 5) {
                    AppText("还有 ${tasks.size - 5} 个历史任务", AppTextRole.CardSubtitle, color = theme.text.copy(alpha = .65f), designScale = scale)
                }
            }
        }
    }
}

private fun taskStatusLabel(status: V25TaskStatus): String = when (status) {
    V25TaskStatus.DRAFT -> "草稿，等待生成"
    V25TaskStatus.SAMPLE_GENERATING -> "正在生成样卡"
    V25TaskStatus.AWAITING_SAMPLE_CONFIRMATION -> "等待确认样卡"
    V25TaskStatus.GENERATING -> "正式生成中"
    V25TaskStatus.COMPLETED -> "已完成"
    V25TaskStatus.FAILED -> "生成失败"
    V25TaskStatus.ABANDONED -> "已放弃"
}

private fun taskStatusColor(status: V25TaskStatus, theme: DeckTheme): Color = when (status) {
    V25TaskStatus.COMPLETED -> Color(0xFFD8F3E2)
    V25TaskStatus.FAILED -> Color(0xFFFFE0DD)
    V25TaskStatus.ABANDONED -> Color(0xFFE8E3EF)
    else -> theme.primary.copy(alpha = .16f)
}

@Composable
private fun ProjectProgressDistribution(scale: Float) = ReviewProgressCard(
    entries = listOf(
        // The review-state bucket distribution is not served for a project either; every
        // column keeps its Figma slot and shows the honest dash.
        ReviewProgressEntry("熟识", AppColors.ReviewKnown, null, 0),
        ReviewProgressEntry("认识", AppColors.ReviewRecognised, null, 0),
        ReviewProgressEntry("模糊", AppColors.ReviewUncertain, null, 0),
        ReviewProgressEntry("陌生", AppColors.ReviewUnfamiliar, null, 0),
        ReviewProgressEntry("没学", AppColors.ReviewUnseen, null, 0)
    ),
    designScale = scale
)

/** Figma 540:4661, the lower pair of project-only summary cards. */
@Composable
private fun ProjectStreakMetrics(scale: Float) = Row(
    Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy((16 * scale).dp)
) {
    // No per-project streak or app-open source exists; keep the slots and show dashes.
    StatisticsMetricCard(
        value = "—",
        kind = StatisticsMetricKind.LongestStreak,
        surface = StatisticsMetricSurface.White,
        designScale = scale,
        modifier = Modifier.weight(1f)
    )
    StatisticsMetricCard(
        value = "—",
        kind = StatisticsMetricKind.OpenCount,
        surface = StatisticsMetricSurface.White,
        designScale = scale,
        modifier = Modifier.weight(1f)
    )
}

@Composable
private fun ProjectDecksContent(
    project: ProjectSummary,
    decks: List<DeckSummary>,
    scale: Float,
    nav: ScreenNavigator,
    onRequestDeleteDeck: (String) -> Unit,
    modifier: Modifier,
) = LazyColumn(
    modifier = modifier.fillMaxWidth().clip(RoundedCornerShape((AppScrollableContentClipRadius * scale).dp)), contentPadding = PaddingValues(bottom = (fixedBottomControlScrollTail(bottomOffset = 16) * scale).dp), verticalArrangement = Arrangement.spacedBy((16 * scale).dp)
) {
    itemsIndexed(decks, key = { _, deck -> deck.id }) { _, deck ->
        val theme = deckTheme(project)
        val progress = deck.masteryRatio ?: if (deck.cardCount == 0) 0f else deck.masteredCards.toFloat() / deck.cardCount
        ProjectSwipeAuto(
            actions = listOf(ProjectSwipeAction("delete", "删除", AppColors.Warning, AppColors.TextIconLight, { onRequestDeleteDeck(deck.id) })),
            scale = scale
        ) {
            ProjectThemedCard(
                title = displayDeckTitle(deck),
                count = deck.cardCount,
                countLabel = "cards",
                progress = progress,
                theme = theme,
                icon = "heap_snapshot_multiple",
                variant = ProjectThemedCardVariant.THEME_BACKGROUND,
                designScale = scale,
                onClick = { nav.navigate(AppRoute.Deck(deck.id)) }
            )
        }
    }
}

/** Figma 494:1447 / 540:3778 deck-management fixed actions. */
@Composable
private fun ProjectDeckActions(
    theme: DeckTheme,
    scale: Float,
    onAddDeck: () -> Unit,
    modifier: Modifier = Modifier
) = Surface(
    onClick = onAddDeck,
    color = theme.primary,
    contentColor = theme.onPrimary,
    shape = RoundedCornerShape((24 * scale).dp),
    modifier = modifier.fillMaxWidth().navigationBarsPadding().padding(horizontal = (16 * scale).dp, vertical = (16 * scale).dp).height((60 * scale).dp)
) {
        Row(Modifier.fillMaxSize(), horizontalArrangement = Arrangement.Center, verticalAlignment = Alignment.CenterVertically) {
            MaterialSymbol("note_stack_add", null, tint = LocalContentColor.current, size = fixedSp(24 * scale), filled = true)
            Spacer(Modifier.width((8 * scale).dp)); AppText("添加卡片组", AppTextRole.Label, color = LocalContentColor.current, designScale = scale, maxLines = 1)
        }
}
