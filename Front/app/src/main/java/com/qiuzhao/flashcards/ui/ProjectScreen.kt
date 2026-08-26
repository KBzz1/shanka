package com.qiuzhao.flashcards.ui

import android.net.Uri
import android.provider.OpenableColumns
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.LocalContentColor
import androidx.compose.material3.Surface
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.zIndex
import com.qiuzhao.flashcards.data.remote.DeckSummary
import com.qiuzhao.flashcards.data.remote.LEGACY_UNASSIGNED_PROJECT_ID
import com.qiuzhao.flashcards.data.remote.ProjectSummary
import com.qiuzhao.flashcards.ui.navigation.AppRoute

/** b944790 project root, bound only to server-backed V2.5 PDF projects. */
@Composable
internal fun ProjectScreen(
    projects: List<ProjectSummary>,
    decks: List<DeckSummary>,
    searchQuery: String,
    viewModel: AppViewModel,
    nav: ScreenNavigator,
) {
    val scale = (LocalConfiguration.current.screenWidthDp / 402f).coerceIn(.75f, 1f)
    val visibleProjects = projects.filter { it.name.contains(searchQuery.trim(), ignoreCase = true) }
    Box(Modifier.fillMaxSize().background(AppColors.BaseBackground).statusBarsPadding()) {
        Column(
            modifier = Modifier.fillMaxSize()
                .padding(start = (16 * scale).dp, top = (88 * scale).dp, end = (16 * scale).dp),
            verticalArrangement = Arrangement.spacedBy((16 * scale).dp),
        ) {
            ProjectRootAction(
                modifier = Modifier.fillMaxWidth(),
                icon = "create_new_folder",
                label = "添加 PDF 项目",
                background = AppColors.Blue.primary,
                content = AppColors.TextIconLight,
                scale = scale,
            ) { nav.navigate(AppRoute.ProjectCreate) }
            LazyColumn(
                modifier = Modifier.weight(1f).fillMaxWidth(),
                verticalArrangement = Arrangement.spacedBy((16 * scale).dp),
                contentPadding = PaddingValues(bottom = (RootNavigationScrollTail * scale).dp),
            ) {
                if (visibleProjects.isEmpty()) item { EmptyProjectCard(scale) }
                items(visibleProjects, key = { it.id }) { project ->
                    ProjectSummaryCard(
                        project,
                        decks.filter { (it.projectId ?: LEGACY_UNASSIGNED_PROJECT_ID) == project.id },
                        scale,
                    ) { nav.navigate(AppRoute.ProjectDetail(project.id)) }
                }
            }
        }
        BottomContentFade(scale, Modifier.align(Alignment.BottomCenter))
    }
}

@Composable
private fun ProjectRootAction(
    modifier: Modifier,
    icon: String,
    label: String,
    background: androidx.compose.ui.graphics.Color,
    content: androidx.compose.ui.graphics.Color,
    scale: Float,
    onClick: () -> Unit,
) = Surface(
    onClick = onClick,
    color = background,
    contentColor = content,
    shape = RoundedCornerShape((24 * scale).dp),
    modifier = modifier.height((60 * scale).dp),
) {
    Row(
        modifier = Modifier.fillMaxSize().padding(horizontal = (24 * scale).dp),
        horizontalArrangement = Arrangement.Center,
        verticalAlignment = Alignment.CenterVertically,
    ) {
        MaterialSymbol(icon, label, tint = LocalContentColor.current, size = fixedSp(24 * scale), filled = true)
        Spacer(Modifier.width((8 * scale).dp))
        AppText(label, AppTextRole.Label, color = LocalContentColor.current, designScale = scale)
    }
}

@Composable
private fun EmptyProjectCard(scale: Float) = Surface(
    color = AppColors.Blue.background,
    shape = RoundedCornerShape((32 * scale).dp),
    modifier = Modifier.fillMaxWidth(),
) {
    Column(
        modifier = Modifier.padding((24 * scale).dp),
        verticalArrangement = Arrangement.spacedBy((8 * scale).dp),
    ) {
        AppText("还没有学习项目", AppTextRole.CardTitle, color = AppColors.Blue.ink, designScale = scale)
        AppText(
            "上传一份 PDF 后可确认章节并开始生成闪卡。",
            AppTextRole.CardSubtitle,
            color = AppColors.Blue.ink.copy(alpha = .7f),
            designScale = scale,
        )
    }
}

@Composable
private fun ProjectSummaryCard(project: ProjectSummary, decks: List<DeckSummary>, scale: Float, onClick: () -> Unit) {
    val theme = deckTheme(project)
    val totalCards = decks.sumOf { it.cardCount }
    val masteredCards = decks.sumOf { it.masteredCards }
    val ratio = if (totalCards == 0) 0f else masteredCards.toFloat() / totalCards
    ProjectThemedCard(
        title = project.name,
        count = project.deckCount,
        countLabel = "group",
        progress = ratio,
        theme = theme,
        icon = "heap_snapshot_multiple",
        variant = ProjectThemedCardVariant.TINTED,
        designScale = scale,
        onClick = onClick,
    )
}

/** The upstream form now accepts one PDF only; no material draft can claim a fake success. */
@Composable
internal fun ProjectCreateScreen(
    viewModel: AppViewModel,
    nav: ScreenNavigator,
    editingProject: ProjectSummary? = null,
) {
    val scale = (LocalConfiguration.current.screenWidthDp / 402f).coerceIn(.75f, 1f)
    val projectId = editingProject?.id
    var name by rememberSaveable(projectId) { mutableStateOf(editingProject?.name.orEmpty()) }
    var selectedPdf by remember { mutableStateOf<Uri?>(null) }
    var selectedPdfName by remember { mutableStateOf<String?>(null) }
    var message by remember { mutableStateOf<String?>(null) }
    val context = LocalContext.current
    val theme = deckTheme(editingProject ?: ProjectSummary("new-project", "新项目"))
    val picker = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        uri?.let {
            selectedPdf = it
            selectedPdfName = projectDocumentName(context, it)
            message = null
        }
    }

    Box(Modifier.fillMaxSize().background(AppColors.BaseBackground)) {
        ScreenTopInformationBar(
            title = if (editingProject == null) "添加 PDF 项目" else "编辑项目",
            subtitle = null,
            onBack = nav::goBack,
            backContainer = theme.secondary,
            titleColor = theme.text,
        )
        LazyColumn(
            modifier = Modifier.fillMaxSize().statusBarsPadding()
                .padding(start = (16 * scale).dp, top = (88 * scale).dp, end = (16 * scale).dp),
            contentPadding = PaddingValues(bottom = (fixedBottomControlScrollTail(bottomOffset = 16) * scale).dp),
            verticalArrangement = Arrangement.spacedBy((16 * scale).dp),
        ) {
            item {
                ProjectCreationPanel(theme, scale) {
                    ProjectSectionLabel("stylus_note", "项目名称", theme, scale)
                    ProjectNameField(name, { name = it }, theme, scale)
                }
            }
            item {
                ProjectCreationPanel(theme, scale) {
                    ProjectSectionLabel("picture_as_pdf", if (editingProject == null) "PDF 学习资料" else "当前资料", theme, scale)
                    if (editingProject == null) {
                        ProjectPdfAction(selectedPdfName, theme, scale) { picker.launch(arrayOf("application/pdf")) }
                        AppText(
                            "每个学习项目仅包含一份 PDF。文本和 Markdown 资料不在本次 Release 中创建。",
                            AppTextRole.CardSubtitle,
                            color = theme.text.copy(alpha = .65f),
                            designScale = scale,
                        )
                    } else {
                        Surface(color = theme.cardPanel, shape = RoundedCornerShape((24 * scale).dp), modifier = Modifier.fillMaxWidth()) {
                            AppText(
                                "项目名称可修改；PDF 内容与章节由服务端管理。",
                                AppTextRole.CardSubtitle,
                                modifier = Modifier.padding((20 * scale).dp),
                                color = theme.text,
                                designScale = scale,
                            )
                        }
                    }
                }
            }
            message?.let { error ->
                item { AppText(error, AppTextRole.CardSubtitle, color = AppColors.WarningStrong, designScale = scale) }
            }
        }
        BottomContentFade(scale, Modifier.align(Alignment.BottomCenter))
        Surface(
            onClick = {
                if (editingProject != null) {
                    viewModel.renameProject(editingProject.id, name) { error ->
                        message = error
                        if (error == null) nav.goBack()
                    }
                } else {
                    val pdf = selectedPdf
                    if (pdf == null) {
                        message = "请选择一份 PDF"
                    } else {
                        viewModel.createProject(pdf, name) { id, error ->
                            message = error
                            if (id != null) nav.replaceTop(AppRoute.ProjectDetail(id))
                        }
                    }
                }
            },
            color = theme.primary,
            contentColor = theme.onPrimary,
            shape = RoundedCornerShape((24 * scale).dp),
            modifier = Modifier.align(Alignment.BottomCenter).navigationBarsPadding()
                .padding(horizontal = (16 * scale).dp, vertical = (16 * scale).dp)
                .fillMaxWidth().height((60 * scale).dp).zIndex(1f),
        ) {
            Row(Modifier.fillMaxSize(), horizontalArrangement = Arrangement.Center, verticalAlignment = Alignment.CenterVertically) {
                MaterialSymbol("list_alt_check", null, tint = LocalContentColor.current, size = fixedSp(24 * scale), filled = true)
                Spacer(Modifier.width((8 * scale).dp))
                AppText(if (editingProject == null) "创建项目" else "保存名称", AppTextRole.Label, color = LocalContentColor.current, designScale = scale)
            }
        }
    }
}

@Composable
private fun ProjectCreationPanel(theme: DeckTheme, scale: Float, content: @Composable ColumnScope.() -> Unit) = Surface(
    color = theme.surface,
    shape = RoundedCornerShape((32 * scale).dp),
    modifier = Modifier.fillMaxWidth(),
) { Column(Modifier.padding((20 * scale).dp), verticalArrangement = Arrangement.spacedBy((16 * scale).dp), content = content) }

@Composable
private fun ProjectSectionLabel(icon: String, label: String, theme: DeckTheme, scale: Float) = Row(
    modifier = Modifier.fillMaxWidth(),
    horizontalArrangement = Arrangement.spacedBy((12 * scale).dp),
    verticalAlignment = Alignment.CenterVertically,
) {
    MaterialSymbol(icon, null, tint = theme.text, size = fixedSp(28 * scale), filled = true)
    AppText(label, AppTextRole.SectionTitle, color = theme.text, designScale = scale)
}

@Composable
private fun ProjectNameField(value: String, onChange: (String) -> Unit, theme: DeckTheme, scale: Float) = Surface(
    color = theme.cardPanel,
    shape = RoundedCornerShape((24 * scale).dp),
    modifier = Modifier.fillMaxWidth().height((59 * scale).dp),
) {
    androidx.compose.foundation.text.BasicTextField(
        value = value,
        onValueChange = onChange,
        singleLine = true,
        textStyle = appInputTextStyle(AppTextRole.Body, scale, theme.text),
        visualTransformation = rememberBilingualInputTransformation(AppTextRole.Body, scale),
        modifier = Modifier.fillMaxSize().padding(horizontal = (24 * scale).dp),
        decorationBox = { input ->
            Box(Modifier.fillMaxSize(), contentAlignment = Alignment.CenterStart) {
                if (value.isBlank()) AppText("此处输入名称", AppTextRole.Body, color = theme.text.copy(alpha = .5f), designScale = scale)
                input()
            }
        },
    )
}

@Composable
private fun ProjectPdfAction(selectedName: String?, theme: DeckTheme, scale: Float, onClick: () -> Unit) = Surface(
    onClick = onClick,
    color = theme.primary,
    contentColor = theme.onPrimary,
    shape = RoundedCornerShape((32 * scale).dp),
    modifier = Modifier.fillMaxWidth().height((80 * scale).dp),
) {
    Row(Modifier.fillMaxSize().padding((12 * scale).dp), verticalAlignment = Alignment.CenterVertically) {
        Surface(color = theme.cardPanel, shape = RoundedCornerShape(999.dp), modifier = Modifier.size((56 * scale).dp)) {
            Box(contentAlignment = Alignment.Center) { MaterialSymbol("picture_as_pdf", null, tint = theme.strongText, size = fixedSp(24 * scale), filled = true) }
        }
        Spacer(Modifier.width((16 * scale).dp))
        Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy((4 * scale).dp)) {
            AppText(selectedName ?: "选择 PDF 文件", AppTextRole.CardTitle, color = LocalContentColor.current, designScale = scale, maxLines = 1, overflow = TextOverflow.Ellipsis)
            AppText("仅支持 PDF", AppTextRole.CardSubtitle, color = theme.cardPanel, designScale = scale)
        }
        MaterialSymbol("arrow_forward", null, tint = LocalContentColor.current, size = fixedSp(24 * scale), filled = true)
    }
}

private fun projectDocumentName(context: android.content.Context, uri: Uri): String =
    context.contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use { cursor ->
        val index = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
        if (index >= 0 && cursor.moveToFirst()) cursor.getString(index) else null
    }.orEmpty().ifBlank { uri.lastPathSegment?.substringAfterLast('/') ?: "未命名 PDF" }
