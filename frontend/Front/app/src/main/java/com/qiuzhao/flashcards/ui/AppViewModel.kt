package com.qiuzhao.flashcards.ui

import android.app.Application
import android.net.Uri
import android.provider.OpenableColumns
import android.util.Log
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import com.qiuzhao.flashcards.BuildConfig
import com.qiuzhao.flashcards.data.CardDraft
import com.qiuzhao.flashcards.data.remote.ApiKeyStatus
import com.qiuzhao.flashcards.data.remote.AuthRepository
import com.qiuzhao.flashcards.data.remote.DeckProgress
import com.qiuzhao.flashcards.data.remote.DeckSummary
import com.qiuzhao.flashcards.data.remote.FlashcardEntity
import com.qiuzhao.flashcards.data.remote.PdfChapter
import com.qiuzhao.flashcards.data.remote.PdfFile
import com.qiuzhao.flashcards.data.remote.Rating
import com.qiuzhao.flashcards.data.session.SessionStore
import com.qiuzhao.flashcards.domain.v25.V25ApiKeyState
import com.qiuzhao.flashcards.domain.v25.V25BrowseFilter
import com.qiuzhao.flashcards.domain.v25.V25BrowseOrder
import com.qiuzhao.flashcards.domain.v25.V25Card
import com.qiuzhao.flashcards.domain.v25.V25CardDeletionBatch
import com.qiuzhao.flashcards.domain.v25.V25CardDraft
import com.qiuzhao.flashcards.domain.v25.V25CoverageMode
import com.qiuzhao.flashcards.domain.v25.V25Deck
import com.qiuzhao.flashcards.domain.v25.V25DeletionPreflight
import com.qiuzhao.flashcards.domain.v25.V25DifficultyRatio
import com.qiuzhao.flashcards.domain.v25.V25ErrorCodes
import com.qiuzhao.flashcards.domain.v25.V25GenerationConfig
import com.qiuzhao.flashcards.domain.v25.V25GenerationTask
import com.qiuzhao.flashcards.domain.v25.V25LearningProject
import com.qiuzhao.flashcards.domain.v25.V25MasteryFilter
import com.qiuzhao.flashcards.domain.v25.V25ProjectStatus
import com.qiuzhao.flashcards.domain.v25.V25ProgressSummary
import com.qiuzhao.flashcards.domain.v25.V25Rating
import com.qiuzhao.flashcards.domain.v25.V25Repository
import com.qiuzhao.flashcards.domain.v25.V25Result
import com.qiuzhao.flashcards.domain.v25.V25SampleCard
import com.qiuzhao.flashcards.domain.v25.V25StatsDashboard
import com.qiuzhao.flashcards.domain.v25.V25StudyPlan
import com.qiuzhao.flashcards.domain.v25.V25StudyPlanUpdate
import com.qiuzhao.flashcards.domain.v25.V25TaskStatus
import com.qiuzhao.flashcards.domain.v25.V25TodayPlan
import com.qiuzhao.flashcards.domain.v25.isAuthFailure
import com.qiuzhao.flashcards.ui.auth.AuthState
import com.qiuzhao.flashcards.ui.auth.AuthViewModel
import com.qiuzhao.flashcards.ui.auth.ErrorMessages
import java.time.Duration
import java.time.Instant
import java.util.UUID
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.collect
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch

/** Configuration captured before samples are requested. Percentages are validated by V2.5. */
data class PdfGenerationConfig(
    val quantity: String = "BALANCED",
    val basic: Float = .4f,
    val understanding: Float = .4f,
    val application: Float = .2f,
    val requirement: String = "",
)

/** Generation inputs selected on the final project UI before the user chooses server chapters. */
internal data class ProjectGenerationDraft(
    val projectId: String,
    val deckName: String,
    val config: PdfGenerationConfig,
)

/**
 * A sample request can outlive the screen that started it.  Reusing only an unfinished task with
 * the same project, deck, chapter snapshot and config lets a retry resume a lost/429 response
 * instead of creating another deck and task.  Terminal tasks and changed inputs intentionally
 * start a new operation.
 */
internal fun reusableSampleTask(
    task: V25GenerationTask?,
    projectId: String,
    deckId: String?,
    chapterIds: List<String>,
    config: V25GenerationConfig,
): V25GenerationTask? = task?.takeIf {
    sampleTaskMatches(it, projectId, deckId, chapterIds, config) &&
        it.status in setOf(
            V25TaskStatus.DRAFT,
            V25TaskStatus.SAMPLE_GENERATING,
            V25TaskStatus.AWAITING_SAMPLE_CONFIRMATION,
        )
}

/** A sample-stage failure can be retried through the server's linked-task endpoint. */
internal fun retryableSampleTask(
    task: V25GenerationTask?,
    projectId: String,
    deckId: String?,
    chapterIds: List<String>,
    config: V25GenerationConfig,
): V25GenerationTask? = task?.takeIf {
    sampleTaskMatches(it, projectId, deckId, chapterIds, config) &&
        it.status == V25TaskStatus.FAILED &&
        it.sampleCards.isEmpty()
}

private fun sampleTaskMatches(
    task: V25GenerationTask,
    projectId: String,
    deckId: String?,
    chapterIds: List<String>,
    config: V25GenerationConfig,
): Boolean =
    task.projectId == projectId &&
        (deckId == null || task.deckId == deckId) &&
        task.selectedChapters.map { chapter -> chapter.id } == chapterIds &&
        task.generationConfig == config

data class LocalAccount(val nickname: String, val email: String)

data class AccountBootstrap(val loaded: Boolean = false, val account: LocalAccount? = null)

data class PdfReadFailure(val title: String, val detail: String)

/**
 * Presentation-only state for the final project/material screens.  A material can be staged
 * before it is uploaded, but the release contract still permits exactly one PDF per project.
 * Text entries therefore never become a pretend server resource.
 */
internal enum class ProjectDraftMaterialType { FILE, TEXT }

internal data class ProjectDraftMaterial(
    val id: String,
    val type: ProjectDraftMaterialType,
    val title: String,
    val extension: String? = null,
    val content: String = "",
    val uri: Uri? = null,
    /** Import time shown on the material card; null for server-less drafts. */
    val importedAt: Instant? = null,
)

private fun ProjectDraftMaterial.renamedFile(rawTitle: String): ProjectDraftMaterial {
    val normalized = rawTitle.trim().ifBlank { title }
    val resolvedExtension = normalized.substringAfterLast('.', extension.orEmpty()).lowercase()
    return copy(
        title = normalized,
        extension = resolvedExtension.ifBlank { extension.orEmpty() }.ifBlank { null },
    )
}

/** Ephemeral handoff from a text parser into the existing deck-import flow. */
data class TextImportFlow(val deckName: String, val cards: List<CardDraft>)

/** Server-derived weekly activity. The UI never derives it from a local review history. */
data class WeeklyActivityData(
    val dailyCounts: List<Int> = List(7) { 0 },
    val total: Int = 0,
    val previousTotal: Int = 0,
    val changePercent: Int? = null,
)

/** Typed presentation projection of V2.5 dashboard data; no screen reads transport JSON. */
data class DashboardUiState(
    val hasData: Boolean = false,
    val weeklyGoal: Int = 0,
    val completed: Int = 0,
    val weeklyGoalRate: Float? = null,
    val recallAccuracy: Float? = null,
    val firstAttemptAccuracy: Float? = null,
    val retentionRate: Float? = null,
    val streakDays: Int = 0,
    val masteredCards: Int = 0,
)

/** Typed projection of the server-computed plan used by the home page. */
data class TodayPlanUiState(
    val dailyGoal: Int = 0,
    val completedCount: Int = 0,
    val dueCount: Int = 0,
    val remainingCount: Int = 0,
    val planConfigured: Boolean = false,
    val selectedDeckIds: List<String> = emptyList(),
    val dailyNewGoal: Int = 10,
    val dailyReviewGoal: Int = 40,
    val newCompletedCount: Int = 0,
    val reviewCompletedCount: Int = 0,
    val newRemainingCount: Int = 0,
    val reviewRemainingCount: Int = 0,
    val coreTargetCount: Int = 0,
    val backlogCount: Int = 0,
)

/** Server-backed study-plan form state. The form itself stays local until save is pressed. */
data class StudyPlanUiState(
    val loaded: Boolean = false,
    val saving: Boolean = false,
    val configured: Boolean = false,
    val currentProjectId: String? = null,
    val selectedDeckIds: List<String> = emptyList(),
    val dailyNewGoal: Int = 10,
    val dailyReviewGoal: Int = 40,
)

/** Pending deletion stays server-authoritative and expires at its returned undo deadline. */
data class PendingDeletionUiState(
    val deleteBatchId: String,
    val undoUntil: Instant,
)

/**
 * Release presentation facade. Upstream Compose screens retain their visual models, while this
 * class is the only place that projects the typed V2.5 repository into those models. It owns
 * loading/empty/error state, idempotent writes and session-expiry recovery; screens never parse
 * HTTP responses or manufacture local records.
 */
class AppViewModel(
    application: Application,
    private val sessionStore: SessionStore,
    /** Kept as `repository` for the existing auth-injection test seam; used only for auth. */
    private val repository: AuthRepository,
    v25Repository: V25Repository,
    /** Process-level session hooks (WorkManager backstop, sync resume/pause). */
    private val onSignedIn: () -> Unit = {},
    private val onSignedOut: () -> Unit = {},
    /** Live deck cache projection; null in JVM tests that inject a plain repository. */
    private val deckUpdates: (() -> Flow<List<V25Deck>>)? = null,
) : AndroidViewModel(application) {
    private val v25Repository: V25Repository = v25Repository
    companion object {
        val Factory: ViewModelProvider.Factory = viewModelFactory {
            initializer {
                val app = this[ViewModelProvider.AndroidViewModelFactory.APPLICATION_KEY]!!
                // Process-level assembly (one OkHttp stack, one shanka-v25.db, one sync
                // coordinator) lives in AppContainer; the ViewModel only picks its members.
                val container = (app as com.qiuzhao.flashcards.FlashcardsApplication).container
                AppViewModel(
                    application = app,
                    sessionStore = container.sessionStore,
                    repository = container.authRepository,
                    v25Repository = container.v25Repository,
                    onSignedIn = container::onUserSignedIn,
                    onSignedOut = container::onUserSignedOut,
                    deckUpdates = container.v25Repository::observeDecks,
                )
            }
        }

        private val projectThemes = listOf("azure", "violet", "green", "orange", "pink")
    }

    /** Auth is separately testable; V2.5 data follows the same bearer session. */
    val auth = AuthViewModel(repository, sessionStore, viewModelScope)
    val authState: StateFlow<AuthState> = auth.state

    private val _accountBootstrap = MutableStateFlow(AccountBootstrap())
    val accountBootstrap: StateFlow<AccountBootstrap> = _accountBootstrap.asStateFlow()

    private val _projects = MutableStateFlow<List<com.qiuzhao.flashcards.data.remote.ProjectSummary>>(emptyList())
    val projects: StateFlow<List<com.qiuzhao.flashcards.data.remote.ProjectSummary>> = _projects.asStateFlow()
    private val projectsById = mutableMapOf<String, V25LearningProject>()
    private val _projectTasks = MutableStateFlow<Map<String, List<V25GenerationTask>>>(emptyMap())
    /** Persisted task snapshots keyed by project; populated on project-detail entry. */
    val projectTasks: StateFlow<Map<String, List<V25GenerationTask>>> = _projectTasks.asStateFlow()
    private val _deletionPreflights = MutableStateFlow<Map<String, V25DeletionPreflight>>(emptyMap())
    val deletionPreflights: StateFlow<Map<String, V25DeletionPreflight>> =
        _deletionPreflights.asStateFlow()
    private val inFlightWrites = mutableSetOf<String>()
    private val writeKeys = mutableMapOf<String, String>()
    private val taskRefreshGeneration = mutableMapOf<String, Long>()
    private val _deletionInFlight = MutableStateFlow<Set<String>>(emptySet())
    val deletionInFlight: StateFlow<Set<String>> = _deletionInFlight.asStateFlow()

    private val _decks = MutableStateFlow<List<DeckSummary>>(emptyList())
    val decks: StateFlow<List<DeckSummary>> = _decks.asStateFlow()
    val dueCount: StateFlow<Int> = _decks
        .map { values -> values.sumOf { it.dueCount } }
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), 0)

    private val cardFlows = mutableMapOf<String, MutableStateFlow<List<FlashcardEntity>>>()
    private val _studyCards = MutableStateFlow<List<FlashcardEntity>>(emptyList())
    val studyCards: StateFlow<List<FlashcardEntity>> = _studyCards.asStateFlow()

    private val _dashboard = MutableStateFlow<DashboardUiState?>(null)
    val dashboard: StateFlow<DashboardUiState?> = _dashboard.asStateFlow()
    private val _weeklyActivity = MutableStateFlow(WeeklyActivityData())
    val weeklyActivity: StateFlow<WeeklyActivityData> = _weeklyActivity.asStateFlow()
    private val _todayPlan = MutableStateFlow(TodayPlanUiState())
    val todayPlan: StateFlow<TodayPlanUiState> = _todayPlan.asStateFlow()
    private val _studyPlan = MutableStateFlow(StudyPlanUiState())
    val studyPlan: StateFlow<StudyPlanUiState> = _studyPlan.asStateFlow()
    private var studyPlanIdempotencyKey: String? = null
    private var studyPlanRequestFingerprint: String? = null
    private val _projectProgress = MutableStateFlow<Map<String, V25ProgressSummary>>(emptyMap())
    val projectProgress: StateFlow<Map<String, V25ProgressSummary>> = _projectProgress.asStateFlow()

    private val _apiKeyStatus = MutableStateFlow<ApiKeyStatus?>(null)
    val apiKeyStatus: StateFlow<ApiKeyStatus?> = _apiKeyStatus.asStateFlow()

    private val _pdfFile = MutableStateFlow<PdfFile?>(null)
    val pdfFile: StateFlow<PdfFile?> = _pdfFile.asStateFlow()
    private val _pdfSamples = MutableStateFlow<List<CardDraft>>(emptyList())
    val pdfSamples: StateFlow<List<CardDraft>> = _pdfSamples.asStateFlow()
    private val _pdfTask = MutableStateFlow<V25GenerationTask?>(null)
    val pdfTask: StateFlow<V25GenerationTask?> = _pdfTask.asStateFlow()
    private val _pdfTaskDeckId = MutableStateFlow<String?>(null)
    val pdfTaskDeckId: StateFlow<String?> = _pdfTaskDeckId.asStateFlow()
    private val _activePdfProject = MutableStateFlow<V25LearningProject?>(null)
    val activePdfProject: StateFlow<V25LearningProject?> = _activePdfProject.asStateFlow()

    private val _projectCreationMaterials = MutableStateFlow<List<ProjectDraftMaterial>>(emptyList())
    internal val projectCreationMaterials: StateFlow<List<ProjectDraftMaterial>> =
        _projectCreationMaterials.asStateFlow()
    private val _projectMaterials = MutableStateFlow<Map<String, List<ProjectDraftMaterial>>>(emptyMap())
    internal val projectMaterials: StateFlow<Map<String, List<ProjectDraftMaterial>>> =
        _projectMaterials.asStateFlow()
    private val _materialImportDrafts = MutableStateFlow<List<ProjectDraftMaterial>>(emptyList())
    internal val materialImportDrafts: StateFlow<List<ProjectDraftMaterial>> =
        _materialImportDrafts.asStateFlow()
    private val _textImportFlow = MutableStateFlow<TextImportFlow?>(null)
    val textImportFlow: StateFlow<TextImportFlow?> = _textImportFlow.asStateFlow()
    private val _projectGenerationDraft = MutableStateFlow<ProjectGenerationDraft?>(null)
    internal val projectGenerationDraft: StateFlow<ProjectGenerationDraft?> =
        _projectGenerationDraft.asStateFlow()

    private val _pendingDeletion = MutableStateFlow<PendingDeletionUiState?>(null)
    val pendingDeletion: StateFlow<PendingDeletionUiState?> = _pendingDeletion.asStateFlow()

    /** One import operation = fixed keys + remembered deck; retries replay only the failed step. */
    private val importCoordinator = ImportCoordinator(v25Repository)
    val importAttempt: StateFlow<ImportAttempt?> = importCoordinator.attempt
    val importSubmitting: StateFlow<Boolean> = importCoordinator.submitting

    /** One rating = fixed client_event_id + idempotency key; retries replay the identical event. */
    private val reviewCoordinator = ReviewCoordinator(v25Repository)
    val reviewAttempt: StateFlow<ReviewAttempt?> = reviewCoordinator.attempt
    val reviewSubmitting: StateFlow<Boolean> = reviewCoordinator.submitting

    /** One PDF upload = fixed idempotency key; retries replay the identical multipart request. */
    private val pdfUploadCoordinator = PdfUploadCoordinator()
    val pdfUploadAttempt: StateFlow<PdfUploadAttempt?> = pdfUploadCoordinator.attempt
    val pdfUploading: StateFlow<Boolean> = pdfUploadCoordinator.uploading

    private val _uiMessage = MutableStateFlow<String?>(null)
    val uiMessage: StateFlow<String?> = _uiMessage.asStateFlow()

    init {
        checkSession()
        viewModelScope.launch {
            auth.state
                .map { it as? AuthState.LoggedIn }
                .distinctUntilChanged()
                .collect { loggedIn ->
                    if (loggedIn == null) {
                        _accountBootstrap.value = AccountBootstrap(loaded = true)
                        onSignedOut()
                        clearAuthenticatedState()
                    } else {
                        _accountBootstrap.value = AccountBootstrap(
                            loaded = true,
                            account = LocalAccount(loggedIn.user.username, ""),
                        )
                        onSignedIn()
                        observeDeckCache()
                        refreshAll()
                    }
                }
        }
    }

    /**
     * The Room deck projection is the live source: the merged refresh after an outbox drain
     * re-emits new due counts here without any per-rating network fan-out.
     */
    private fun observeDeckCache() {
        val source = deckUpdates ?: return
        viewModelScope.launch {
            source().collect { decks -> _decks.value = decks.map(::toDeckSummary) }
        }
    }

    fun checkSession() = auth.checkSession()

    fun clearAuthError() = auth.clearError()

    fun clearUiMessage() {
        _uiMessage.value = null
    }

    /** Auth screen delegates here so login/register stay on the bearer-session path. */
    fun login(email: String, password: String, onResult: (String?) -> Unit) = viewModelScope.launch {
        if (email.isBlank() || password.isBlank() || auth.submitting.value) return@launch
        onResult(auth.submitLogin(email.trim(), password))
    }

    fun register(
        username: String,
        email: String,
        password: String,
        confirmation: String,
        onResult: (String?) -> Unit,
    ) = viewModelScope.launch {
        if (username.isBlank() || email.isBlank() || password.isBlank() || auth.submitting.value) return@launch
        if (!AuthViewModel.passwordsMatch(password, confirmation)) {
            onResult(AuthViewModel.PASSWORD_MISMATCH_MESSAGE)
            return@launch
        }
        onResult(auth.submitRegister(username.trim(), email.trim(), password))
    }

    /** Local-first sign out and token revocation are owned by [AuthViewModel]. */
    fun logout() = auth.logout()

    /**
     * Hydrates the signed-in home state without making the app trip the global
     * server IP limiter. Session restoration has already issued one /auth/me
     * request, so the first wave contains only the three states needed to render
     * the library. The secondary panels are intentionally deferred to the next
     * one-second window; neither wave waits on an unrelated panel request.
     */
    fun refreshAll() {
        refreshProjects()
        refreshDecks()
        refreshStudyPlan()
        refreshTodayPlan()
        viewModelScope.launch {
            delay(1_100)
            refreshDashboard()
            refreshApiKeyStatus()
            recoverPendingDeletion()
            refreshProfile()
        }
    }

    fun refreshProfile(): Job = viewModelScope.launch {
        when (val result = v25Repository.getAuthUser()) {
            is V25Result.Success -> _accountBootstrap.value = AccountBootstrap(
                loaded = true,
                account = LocalAccount(result.value.username, result.value.email),
            )
            is V25Result.Failure -> handleFailure("get_auth_user", result, surface = false)
        }
    }

    fun refreshProjects(): Job = viewModelScope.launch {
        when (val result = v25Repository.listProjects()) {
            is V25Result.Success -> {
                projectsById.clear()
                result.value.forEach { project -> projectsById[project.projectId] = project }
                _projects.value = result.value.map(::toProjectSummary)
                syncProjectMaterials(result.value)
            }
            is V25Result.Failure -> handleFailure("list_projects", result)
        }
    }

    /** Loads every persisted task for a project; unlike [_pdfTask], this survives app restarts. */
    fun refreshProjectTasks(projectId: String): Job = viewModelScope.launch {
        val requestGeneration = (taskRefreshGeneration[projectId] ?: 0L) + 1L
        taskRefreshGeneration[projectId] = requestGeneration
        when (val result = v25Repository.listTasks(projectId = projectId)) {
            is V25Result.Success -> {
                if (taskRefreshGeneration[projectId] == requestGeneration) {
                    _projectTasks.value = _projectTasks.value + (projectId to result.value)
                }
            }
            is V25Result.Failure -> handleFailure("list_project_tasks", result)
        }
    }

    /** Refreshes the advisory deletion preview used by the confirmation dialog. */
    fun refreshProjectDeletionPreflight(
        projectId: String,
        retainDecks: Boolean,
        allowCancel: Boolean = true,
        onResult: (V25DeletionPreflight?) -> Unit = {},
    ): Job = viewModelScope.launch {
        when (val result = v25Repository.getProjectDeletionPreflight(projectId, retainDecks, allowCancel)) {
            is V25Result.Success -> {
                val key = projectDeletionKey(projectId, retainDecks)
                _deletionPreflights.value = _deletionPreflights.value + (key to result.value)
                onResult(result.value)
            }
            is V25Result.Failure -> {
                handleFailure("project_deletion_preflight", result, surface = false)
                onResult(null)
            }
        }
    }

    /** Refreshes the advisory deletion preview used by deck confirmation affordances. */
    fun refreshDeckDeletionPreflight(
        deckId: String,
        allowCancel: Boolean = true,
        onResult: (V25DeletionPreflight?) -> Unit = {},
    ): Job = viewModelScope.launch {
        when (val result = v25Repository.getDeckDeletionPreflight(deckId, allowCancel)) {
            is V25Result.Success -> {
                _deletionPreflights.value = _deletionPreflights.value + (deckDeletionKey(deckId) to result.value)
                onResult(result.value)
            }
            is V25Result.Failure -> {
                handleFailure("deck_deletion_preflight", result, surface = false)
                onResult(null)
            }
        }
    }

    /** Compose-facing lookup keeps map-key conventions out of screens. */
    fun projectDeletionPreflightKey(projectId: String, retainDecks: Boolean): String =
        projectDeletionKey(projectId, retainDecks)

    /** Compose-facing lookup for a deck preview kept in the shared deletion map. */
    fun deckDeletionPreflightKey(deckId: String): String = deckDeletionKey(deckId)

    fun refreshDecks(): Job = viewModelScope.launch {
        when (val result = v25Repository.listDecks()) {
            is V25Result.Success -> _decks.value = result.value.map(::toDeckSummary)
            is V25Result.Failure -> handleFailure("list_decks", result)
        }
    }

    fun refreshDashboard(): Job = viewModelScope.launch {
        when (val result = v25Repository.statsDashboard()) {
            is V25Result.Success -> setDashboard(result.value)
            is V25Result.Failure -> handleFailure("stats_dashboard", result)
        }
    }

    fun refreshTodayPlan(): Job = viewModelScope.launch {
        when (val result = v25Repository.todayPlan()) {
            is V25Result.Success -> setTodayPlan(result.value)
            is V25Result.Failure -> handleFailure("today_plan", result)
        }
    }

    fun refreshStudyPlan(): Job = viewModelScope.launch {
        when (val result = v25Repository.getStudyPlan()) {
            is V25Result.Success -> setStudyPlan(result.value)
            is V25Result.Failure -> handleFailure("study_plan", result)
        }
    }

    /** Loads the server-computed lifecycle aggregate for a project detail page. */
    fun refreshProjectProgress(projectId: String): Job = viewModelScope.launch {
        when (val result = v25Repository.projectProgress(projectId)) {
            is V25Result.Success -> {
                _projectProgress.value = _projectProgress.value + (projectId to result.value)
            }
            is V25Result.Failure -> handleFailure("project_progress", result, surface = false)
        }
    }

    /** Saves the whole plan once; a failed response leaves the caller's local form untouched. */
    fun saveStudyPlan(
        currentProjectId: String,
        selectedDeckIds: List<String>,
        dailyNewGoal: Int,
        dailyReviewGoal: Int,
        onSuccess: () -> Unit = {},
    ): Job = viewModelScope.launch {
        if (_studyPlan.value.saving) return@launch
        _studyPlan.value = _studyPlan.value.copy(saving = true)
        val fingerprint = listOf(
            currentProjectId,
            selectedDeckIds.joinToString(","),
            dailyNewGoal.toString(),
            dailyReviewGoal.toString(),
        ).joinToString("|")
        val key = if (studyPlanIdempotencyKey == null || studyPlanRequestFingerprint != fingerprint) {
            UUID.randomUUID().toString().also {
                studyPlanIdempotencyKey = it
                studyPlanRequestFingerprint = fingerprint
            }
        } else {
            studyPlanIdempotencyKey!!
        }
        val result = v25Repository.updateStudyPlan(
            V25StudyPlanUpdate(currentProjectId, selectedDeckIds, dailyNewGoal, dailyReviewGoal),
            idempotencyKey = key,
        )
        when (result) {
            is V25Result.Success -> {
                studyPlanIdempotencyKey = null
                studyPlanRequestFingerprint = null
                setStudyPlan(result.value)
                refreshDecks()
                refreshTodayPlan()
                onSuccess()
            }
            is V25Result.Failure -> handleFailure("update_study_plan", result)
        }
        _studyPlan.value = _studyPlan.value.copy(saving = false)
    }

    fun attachDeckToProject(projectId: String, deckId: String, onSuccess: () -> Unit = {}): Job = viewModelScope.launch {
        when (val result = v25Repository.attachDeckToProject(projectId, deckId, UUID.randomUUID().toString())) {
            is V25Result.Success -> {
                refreshDecks()
                refreshStudyPlan()
                onSuccess()
            }
            is V25Result.Failure -> handleFailure("attach_deck_to_project", result)
        }
    }

    /** Starts a new project form; the form can stage one PDF before the upload is committed. */
    fun resetProjectCreationDraft() {
        _projectCreationMaterials.value = emptyList()
        _materialImportDrafts.value = emptyList()
    }

    /**
     * Stores a URI only until the user confirms project creation.  Keeping the URI instead of
     * just a display name is essential: the previous final-UI implementation could only create
     * a local mock because the actual PDF bytes had already been discarded.
     */
    fun addProjectDraftFile(uri: Uri, displayName: String = contentResolver.displayName(uri)) {
        val extension = displayName.substringAfterLast('.', "").lowercase()
        if (extension != "pdf") {
            _uiMessage.value = "当前项目仅支持导入一份 PDF"
            return
        }
        _projectCreationMaterials.value = listOf(
            ProjectDraftMaterial(
                id = "project-pdf-${System.nanoTime()}",
                type = ProjectDraftMaterialType.FILE,
                title = displayName,
                extension = extension,
                uri = uri,
            ),
        )
    }

    /** Kept for an old visual callback; it intentionally refuses name-only fake files. */
    fun addProjectDraftFile(@Suppress("UNUSED_PARAMETER") displayName: String) {
        _uiMessage.value = "请通过文件选择器选择 PDF，不能只保存文件名"
    }

    fun deleteProjectDraftMaterial(materialId: String) {
        _projectCreationMaterials.value = _projectCreationMaterials.value.filterNot { it.id == materialId }
    }

    internal fun renameProjectDraftFile(materialId: String, title: String) {
        _projectCreationMaterials.value = _projectCreationMaterials.value.map { material ->
            if (material.id == materialId) material.renamedFile(title) else material
        }
    }

    /** Text and multi-file materials have no backend contract yet, so do not retain fake data. */
    fun upsertProjectDraftText(
        @Suppress("UNUSED_PARAMETER") materialId: String?,
        @Suppress("UNUSED_PARAMETER") title: String,
        @Suppress("UNUSED_PARAMETER") content: String,
    ) {
        _uiMessage.value = "当前服务只支持 PDF 资料，不支持文本资料"
    }

    internal fun projectMaterialList(projectId: String): List<ProjectDraftMaterial> =
        _projectMaterials.value[projectId].orEmpty()

    internal fun deleteProjectMaterial(
        @Suppress("UNUSED_PARAMETER") projectId: String,
        @Suppress("UNUSED_PARAMETER") materialId: String,
    ) {
        _uiMessage.value = "项目 PDF 不能单独删除；如需移除，请删除项目或在解析失败后替换 PDF"
    }

    internal fun upsertProjectMaterial(
        @Suppress("UNUSED_PARAMETER") projectId: String,
        @Suppress("UNUSED_PARAMETER") material: ProjectDraftMaterial,
    ) {
        _uiMessage.value = "当前项目不支持追加资料；每个项目仅绑定一份 PDF"
    }

    internal fun renameProjectFile(
        @Suppress("UNUSED_PARAMETER") projectId: String,
        @Suppress("UNUSED_PARAMETER") materialId: String,
        @Suppress("UNUSED_PARAMETER") title: String,
    ) {
        _uiMessage.value = "PDF 文件名由服务端管理；可在项目编辑页修改项目名称"
    }

    internal fun beginMaterialImport() {
        _materialImportDrafts.value = emptyList()
    }

    internal fun stageMaterialImportFiles(uris: List<Uri>) {
        val uri = uris.singleOrNull()
        if (uri == null) {
            _uiMessage.value = "一次只能选择一份 PDF"
            return
        }
        val displayName = contentResolver.displayName(uri)
        val extension = displayName.substringAfterLast('.', "").lowercase()
        if (extension != "pdf") {
            _uiMessage.value = "当前项目仅支持 PDF"
            return
        }
        _materialImportDrafts.value = listOf(
            ProjectDraftMaterial(
                id = "staged-pdf-${System.nanoTime()}",
                type = ProjectDraftMaterialType.FILE,
                title = displayName,
                extension = extension,
                uri = uri,
            ),
        )
    }

    internal fun stageMaterialImportText(
        @Suppress("UNUSED_PARAMETER") title: String,
        @Suppress("UNUSED_PARAMETER") content: String,
    ) {
        _uiMessage.value = "当前服务只支持 PDF 资料，不支持文本资料"
    }

    internal fun upsertMaterialImportText(
        @Suppress("UNUSED_PARAMETER") materialId: String?,
        @Suppress("UNUSED_PARAMETER") title: String,
        @Suppress("UNUSED_PARAMETER") content: String,
    ) {
        _uiMessage.value = "当前服务只支持 PDF 资料，不支持文本资料"
    }

    internal fun removeMaterialImportDraft(materialId: String) {
        _materialImportDrafts.value = _materialImportDrafts.value.filterNot { it.id == materialId }
    }

    internal fun renameMaterialImportFile(materialId: String, title: String) {
        _materialImportDrafts.value = _materialImportDrafts.value.map { material ->
            if (material.id == materialId) material.renamedFile(title) else material
        }
    }

    /**
     * Commits staged content only through a real service operation.  New projects retain the
     * URI until the form is submitted; existing projects can replace their file only when the
     * service accepts a replace-PDF transition.
     */
    internal fun commitMaterialImport(
        projectId: String?,
        onResult: (success: Boolean, message: String?) -> Unit = { _, _ -> },
    ) {
        val staged = _materialImportDrafts.value.singleOrNull() ?: run {
            _uiMessage.value = "请先选择一份 PDF"
            onResult(false, "请先选择一份 PDF")
            return
        }
        val uri = staged.uri ?: run {
            _uiMessage.value = "无法读取所选 PDF"
            onResult(false, "无法读取所选 PDF")
            return
        }
        if (projectId == null) {
            _projectCreationMaterials.value = listOf(staged)
            _materialImportDrafts.value = emptyList()
            onResult(true, null)
            return
        }
        replaceProjectPdf(projectId, uri) { success, message ->
            if (success) _materialImportDrafts.value = emptyList()
            else _uiMessage.value = message ?: "当前项目不能替换 PDF"
            onResult(success, message)
        }
    }

    /** Creates the project form's staged PDF through multipart POST /projects. */
    fun createProjectFromDraft(
        name: String,
        @Suppress("UNUSED_PARAMETER") themeKey: String,
        onResult: (String?) -> Unit,
    ) {
        val material = _projectCreationMaterials.value.singleOrNull()
        val uri = material?.uri
        if (uri == null) {
            onResult("请先选择一份 PDF")
            return
        }
        createProject(uri, name) { _, error ->
            if (error == null) {
                _projectCreationMaterials.value = emptyList()
                _materialImportDrafts.value = emptyList()
            }
            onResult(error)
        }
    }

    /** Renames an existing project through PATCH /projects/{project_id}. */
    fun renameProjectFromEditor(
        projectId: String,
        name: String,
        @Suppress("UNUSED_PARAMETER") themeKey: String,
        onResult: (String?) -> Unit,
    ) = renameProject(projectId, name, onResult)

    /**
     * Compatibility entry point for the pre-project PDF wizard.  It now creates a real V2.5
     * project and waits for its server-side parse result; it never manufactures chapters from
     * the filename or stores a local-only document.
     */
    fun uploadPdf(
        uri: Uri,
        onParsed: (List<PdfChapter>) -> Unit,
        onFailure: (PdfReadFailure) -> Unit,
    ) = viewModelScope.launch {
        val fileName = contentResolver.displayName(uri)
        // One user operation owns its fixed Idempotency-Key; the retry reopens the stream
        // (an InputStream is single-use) but replays the identical multipart request.
        val attempt = pdfUploadCoordinator.begin(PdfUploadOperation.CreateProject(null), uri.toString(), fileName)
        if (attempt == null) {
            onFailure(PdfReadFailure("上传进行中", "当前已在处理一个 PDF 上传，请稍候再试。"))
            return@launch
        }
        val input = contentResolver.openInputStream(uri)
        if (input == null) {
            pdfUploadCoordinator.fail()
            onFailure(PdfReadFailure("无法读取 PDF", "所选文件无法打开，请重新选择。"))
            return@launch
        }
        input.use { content ->
            val projectName = fileName.substringBeforeLast('.').trim().ifBlank { null }
            when (val result = v25Repository.createProject(fileName, content, projectName, idempotencyKey = attempt.idempotencyKey)) {
                is V25Result.Success -> {
                    pdfUploadCoordinator.commit()
                    projectsById[result.value.projectId] = result.value
                    syncProjectMaterial(result.value)
                    _activePdfProject.value = result.value
                    _pdfFile.value = result.value.toPdfFile()
                    refreshProjects()
                    awaitPdfParse(result.value.projectId, onParsed, onFailure)
                }
                is V25Result.Failure -> {
                    pdfUploadCoordinator.fail()
                    handleFailure("create_project_for_pdf_wizard", result, surface = false)
                    onFailure(PdfReadFailure("上传失败", userMessage(result)))
                }
            }
        }
    }

    /** The parser is asynchronous, so a parse-in-progress response is not mistaken for success. */
    private suspend fun awaitPdfParse(
        projectId: String,
        onParsed: (List<PdfChapter>) -> Unit,
        onFailure: (PdfReadFailure) -> Unit,
    ) {
        repeat(120) {
            when (val result = v25Repository.getProject(projectId)) {
                is V25Result.Success -> {
                    projectsById[projectId] = result.value
                    syncProjectMaterial(result.value)
                    _activePdfProject.value = result.value
                    _pdfFile.value = result.value.toPdfFile()
                    when (result.value.status) {
                        V25ProjectStatus.PARSING -> delay(1_000)
                        V25ProjectStatus.AWAITING_CHAPTER_CONFIRMATION,
                        V25ProjectStatus.READY -> {
                            onParsed(_pdfFile.value?.chapters.orEmpty())
                            return
                        }
                        V25ProjectStatus.PARSE_FAILED -> {
                            onFailure(
                                PdfReadFailure(
                                    "PDF 解析失败",
                                    result.value.file.errorCode ?: "服务未能解析该 PDF，请替换文件后重试。",
                                ),
                            )
                            return
                        }
                    }
                }
                is V25Result.Failure -> {
                    handleFailure("poll_project_parse", result, surface = false)
                    onFailure(PdfReadFailure("无法获取解析结果", userMessage(result)))
                    return
                }
            }
        }
        onFailure(PdfReadFailure("解析仍在进行", "解析超过两分钟仍未完成，请稍后从项目页继续查看。"))
    }

    /** Project creation accepts exactly one PDF; text/Markdown are not a Release API. */
    fun createProject(uri: Uri, name: String, onResult: (String?, String?) -> Unit) = viewModelScope.launch {
        val fileName = contentResolver.displayName(uri)
        val attempt = pdfUploadCoordinator.begin(
            PdfUploadOperation.CreateProject(name.trim().ifBlank { null }),
            uri.toString(),
            fileName,
        )
        if (attempt == null) {
            onResult(null, "上传进行中，请稍候再试")
            return@launch
        }
        val input = contentResolver.openInputStream(uri)
        if (input == null) {
            pdfUploadCoordinator.fail()
            onResult(null, "无法读取所选 PDF")
            return@launch
        }
        input.use { content ->
            when (val result = v25Repository.createProject(fileName, content, (attempt.operation as PdfUploadOperation.CreateProject).name, idempotencyKey = attempt.idempotencyKey)) {
                is V25Result.Success -> {
                    pdfUploadCoordinator.commit()
                    projectsById[result.value.projectId] = result.value
                    syncProjectMaterial(result.value)
                    _activePdfProject.value = result.value
                    _pdfFile.value = result.value.toPdfFile()
                    refreshProjects()
                    onResult(result.value.projectId, null)
                }
                is V25Result.Failure -> {
                    pdfUploadCoordinator.fail()
                    handleFailure("create_project", result, surface = false)
                    onResult(null, userMessage(result))
                }
            }
        }
    }

    /** Replaces the only PDF owned by a project after a parse failure. */
    fun replaceProjectPdf(projectId: String, uri: Uri, onResult: (Boolean, String?) -> Unit) = viewModelScope.launch {
        val fileName = contentResolver.displayName(uri)
        val attempt = pdfUploadCoordinator.begin(PdfUploadOperation.ReplacePdf(projectId), uri.toString(), fileName)
        if (attempt == null) {
            onResult(false, "上传进行中，请稍候再试")
            return@launch
        }
        val input = contentResolver.openInputStream(uri)
        if (input == null) {
            pdfUploadCoordinator.fail()
            onResult(false, "无法读取所选 PDF")
            return@launch
        }
        input.use { content ->
            when (val result = v25Repository.replaceProjectPdf(projectId, fileName, content, idempotencyKey = attempt.idempotencyKey)) {
                is V25Result.Success -> {
                    pdfUploadCoordinator.commit()
                    projectsById[projectId] = result.value
                    syncProjectMaterial(result.value)
                    _activePdfProject.value = result.value
                    _pdfFile.value = result.value.toPdfFile()
                    refreshProjects()
                    onResult(true, null)
                }
                is V25Result.Failure -> {
                    pdfUploadCoordinator.fail()
                    handleFailure("replace_project_pdf", result, surface = false)
                    onResult(false, userMessage(result))
                }
            }
        }
    }

    fun renameProject(projectId: String, name: String, onResult: (String?) -> Unit) = viewModelScope.launch {
        val normalized = name.trim()
        if (normalized.isBlank()) {
            onResult("请填写项目名称")
            return@launch
        }
        when (val result = v25Repository.renameProject(projectId, normalized)) {
            is V25Result.Success -> {
                projectsById[projectId] = result.value
                syncProjectMaterial(result.value)
                if (_activePdfProject.value?.projectId == projectId) _activePdfProject.value = result.value
                _pdfFile.value = _activePdfProject.value?.toPdfFile()
                refreshProjects()
                onResult(null)
            }
            is V25Result.Failure -> {
                handleFailure("rename_project", result, surface = false)
                onResult(userMessage(result))
            }
        }
    }

    fun deleteProject(
        projectId: String,
        retainDecks: Boolean,
        onResult: (Boolean) -> Unit = {},
    ) = deleteProjectInternal(projectId, retainDecks, false, false, onResult)

    fun deleteProject(
        projectId: String,
        retainDecks: Boolean,
        abandonPreGenerationTasks: Boolean,
        onResult: (Boolean) -> Unit = {},
        cancelActiveTasks: Boolean = false,
    ) = deleteProjectInternal(
        projectId,
        retainDecks,
        abandonPreGenerationTasks,
        cancelActiveTasks,
        onResult,
    )

    private fun deleteProjectInternal(
        projectId: String,
        retainDecks: Boolean,
        abandonPreGenerationTasks: Boolean,
        cancelActiveTasks: Boolean,
        onResult: (Boolean) -> Unit,
    ) = viewModelScope.launch {
        val operation = projectDeletionOperationKey(
            projectId,
            retainDecks,
            abandonPreGenerationTasks,
            cancelActiveTasks,
        )
        val idempotencyKey = beginWrite(operation)
        if (idempotencyKey == null) {
            onResult(false)
            return@launch
        }
        var succeeded = false
        try {
            when (
                val result = v25Repository.deleteProject(
                    projectId,
                    retainDecks,
                    abandonPreGenerationTasks,
                    idempotencyKey,
                    cancelActiveTasks,
                )
            ) {
                is V25Result.Success -> {
                    succeeded = true
                    projectsById.remove(projectId)
                    _projectTasks.value = _projectTasks.value - projectId
                    _projectProgress.value = _projectProgress.value - projectId
                    _deletionPreflights.value = _deletionPreflights.value
                        .filterKeys { !it.startsWith("project:$projectId:") }
                    if (_activePdfProject.value?.projectId == projectId) clearPdfFlow()
                    refreshProjects()
                    refreshDecks()
                    refreshTodayPlan()
                    onResult(true)
                }
                is V25Result.Failure -> {
                    handleFailure("delete_project", result)
                    onResult(false)
                }
            }
        } finally {
            finishWrite(operation, succeeded)
        }
    }

    /** Captures visual form choices, then hydrates the real project before chapter selection. */
    internal fun prepareProjectGeneration(
        projectId: String,
        deckName: String,
        config: PdfGenerationConfig,
        onReady: (Boolean) -> Unit,
    ) {
        _projectGenerationDraft.value = ProjectGenerationDraft(projectId, deckName, config)
        openProjectForGeneration(projectId, onReady)
    }

    /** Opens a real project for chapter confirmation and generation. */
    fun openProjectForGeneration(projectId: String, onReady: (Boolean) -> Unit) = viewModelScope.launch {
        when (val result = v25Repository.getProject(projectId)) {
            is V25Result.Success -> {
                projectsById[projectId] = result.value
                syncProjectMaterial(result.value)
                _activePdfProject.value = result.value
                _pdfFile.value = result.value.toPdfFile()
                onReady(true)
            }
            is V25Result.Failure -> {
                handleFailure("get_project", result)
                onReady(false)
            }
        }
    }

    /** Refreshes active project state while parsing or after leaving a background task. */
    fun refreshActiveProject(onUpdated: (V25LearningProject) -> Unit = {}) = viewModelScope.launch {
        val projectId = _activePdfProject.value?.projectId ?: return@launch
        when (val result = v25Repository.getProject(projectId)) {
            is V25Result.Success -> {
                projectsById[projectId] = result.value
                syncProjectMaterial(result.value)
                _activePdfProject.value = result.value
                _pdfFile.value = result.value.toPdfFile()
                refreshProjects()
                onUpdated(result.value)
            }
            is V25Result.Failure -> handleFailure("get_project", result)
        }
    }

    fun confirmPdfChapters(onResult: (String?) -> Unit) = viewModelScope.launch {
        val project = _activePdfProject.value
        if (project == null) {
            onResult("请先选择一个 PDF 项目")
            return@launch
        }
        when (val result = v25Repository.confirmChapters(project.projectId)) {
            is V25Result.Success -> {
                projectsById[project.projectId] = result.value
                syncProjectMaterial(result.value)
                _activePdfProject.value = result.value
                _pdfFile.value = result.value.toPdfFile()
                refreshProjects()
                onResult(null)
            }
            is V25Result.Failure -> {
                handleFailure("confirm_chapters", result, surface = false)
                onResult(userMessage(result))
            }
        }
    }

    fun updatePdfChapter(chapter: PdfChapter, onFailure: () -> Unit = {}) = viewModelScope.launch {
        val project = _activePdfProject.value ?: return@launch
        when (val result = v25Repository.updateChapter(
            project.projectId,
            chapter.id,
            com.qiuzhao.flashcards.domain.v25.V25ChapterEdit(chapter.name.trim(), chapter.startPage, chapter.endPage),
        )) {
            is V25Result.Success -> refreshActiveProject()
            is V25Result.Failure -> {
                handleFailure("update_chapter", result)
                onFailure()
            }
        }
    }

    fun deletePdfChapter(chapter: PdfChapter, onFailure: () -> Unit = {}) = viewModelScope.launch {
        val project = _activePdfProject.value ?: return@launch
        when (val result = v25Repository.deleteChapter(project.projectId, chapter.id, deleteCards = false)) {
            is V25Result.Success -> refreshActiveProject()
            is V25Result.Failure -> {
                handleFailure("delete_chapter", result)
                onFailure()
            }
        }
    }

    /**
     * Creates the destination deck (if necessary), then the task and persisted samples. No cards
     * are visible until [startPdfTask] finishes its atomically published generation.
     */
    fun generatePdfSamples(
        existingDeckId: String?,
        deckName: String,
        chapterIds: List<String>,
        config: PdfGenerationConfig,
        onReady: () -> Unit,
        onFailure: (String?) -> Unit = {},
    ) = viewModelScope.launch {
        val currentProject = _activePdfProject.value ?: run {
            onFailure("PDF_NOT_READY")
            return@launch
        }
        if (chapterIds.isEmpty()) {
            onFailure("CHAPTER_NOT_FOUND")
            return@launch
        }
        val project = when (currentProject.status) {
            V25ProjectStatus.AWAITING_CHAPTER_CONFIRMATION -> when (
                val confirmed = v25Repository.confirmChapters(currentProject.projectId)
            ) {
                is V25Result.Success -> {
                    projectsById[confirmed.value.projectId] = confirmed.value
                    syncProjectMaterial(confirmed.value)
                    _activePdfProject.value = confirmed.value
                    _pdfFile.value = confirmed.value.toPdfFile()
                    confirmed.value
                }
                is V25Result.Failure -> {
                    handleFailure("confirm_chapters_for_generation", confirmed, surface = false)
                    onFailure(confirmed.code)
                    return@launch
                }
            }
            V25ProjectStatus.READY -> currentProject
            else -> {
                onFailure("PDF_NOT_READY")
                return@launch
            }
        }
        val generationConfig = V25GenerationConfig(coverageMode(config.quantity), difficultyRatio(config), config.requirement.trim())
        val pending = reusableSampleTask(
            task = _pdfTask.value,
            projectId = project.projectId,
            deckId = existingDeckId,
            chapterIds = chapterIds,
            config = generationConfig,
        )
        val failedSample = retryableSampleTask(
            task = _pdfTask.value,
            projectId = project.projectId,
            deckId = existingDeckId,
            chapterIds = chapterIds,
            config = generationConfig,
        )
        val deckId = existingDeckId ?: pending?.deckId ?: failedSample?.deckId ?: when (val created = v25Repository.createDeck(
            deckName.trim().ifBlank { "${project.name} 卡片组" },
            project.projectId,
        )) {
            is V25Result.Success -> created.value.deckId
            is V25Result.Failure -> {
                handleFailure("create_deck_for_task", created, surface = false)
                onFailure(created.code)
                return@launch
            }
        }
        val task = pending ?: if (failedSample != null) {
            when (val retried = v25Repository.retryTask(failedSample.taskId)) {
                is V25Result.Success -> retried.value
                is V25Result.Failure -> {
                    handleFailure("retry_sample_task", retried, surface = false)
                    onFailure(retried.code)
                    return@launch
                }
            }
        } else when (val created = v25Repository.createTask(project.projectId, deckId, chapterIds, generationConfig)) {
            is V25Result.Failure -> {
                handleFailure("create_task", created, surface = false)
                onFailure(created.code)
                return@launch
            }
            is V25Result.Success -> created.value
        }
        _pdfTask.value = task
        _pdfTaskDeckId.value = task.deckId ?: deckId

        suspend fun deliverSamples(candidate: V25GenerationTask) {
            // The worker is server-owned; even an acknowledged POST may still have no cards.
            when (val ready = awaitSampleCards(candidate)) {
                is V25Result.Success -> {
                    _pdfSamples.value = ready.value.map { sample -> CardDraft(sample.front, sample.back) }
                    refreshDecks()
                    onReady()
                }
                is V25Result.Failure -> {
                    handleFailure("await_samples", ready, surface = false)
                    onFailure(ready.code)
                }
            }
        }

        when (task.status) {
            V25TaskStatus.SAMPLE_GENERATING,
            V25TaskStatus.AWAITING_SAMPLE_CONFIRMATION -> deliverSamples(task)
            V25TaskStatus.DRAFT -> when (val samples = v25Repository.generateSamples(task.taskId)) {
                is V25Result.Success -> deliverSamples(task)
                is V25Result.Failure -> {
                    // The request may have reached the server just before the response was
                    // lost. Re-read once on a state conflict and continue waiting instead of
                    // reporting a false failure or creating a duplicate task on retry.
                    if (samples.code == "TASK_STATE_CONFLICT") {
                        when (val refreshed = v25Repository.getTask(task.taskId)) {
                            is V25Result.Success -> when (refreshed.value.status) {
                                V25TaskStatus.SAMPLE_GENERATING,
                                V25TaskStatus.AWAITING_SAMPLE_CONFIRMATION -> deliverSamples(refreshed.value)
                                else -> {
                                    handleFailure("generate_samples", samples, surface = false)
                                    onFailure(samples.code)
                                }
                            }
                            is V25Result.Failure -> {
                                handleFailure("refresh_samples_task", refreshed, surface = false)
                                onFailure(refreshed.code)
                            }
                        }
                    } else {
                        handleFailure("generate_samples", samples, surface = false)
                        onFailure(samples.code)
                    }
                }
            }
            else -> {
                handleFailure(
                    "generate_samples",
                    V25Result.Failure(V25ErrorCodes.TASK_STATE_CONFLICT),
                    surface = false,
                )
                onFailure(V25ErrorCodes.TASK_STATE_CONFLICT)
            }
        }
    }

    /** Legacy wizard overload: samples belong to the active project's real V2.5 task. */
    fun generatePdfSamples(
        chapterIds: List<String>,
        config: PdfGenerationConfig,
        onReady: () -> Unit,
        onFailure: (String?) -> Unit = {},
    ) {
        val projectName = _activePdfProject.value?.name.orEmpty()
        generatePdfSamples(null, "${projectName.ifBlank { "PDF" }} 卡片组", chapterIds, config, onReady, onFailure)
    }

    /** Starts sample-confirmed generation. Leaving the screen is safe; polling resumes on re-entry. */
    fun startPdfTask(onStarted: () -> Unit, onFailure: (String?) -> Unit = {}) = viewModelScope.launch {
        val task = _pdfTask.value ?: run {
            onFailure("TASK_NOT_FOUND")
            return@launch
        }
        when (val result = v25Repository.startTask(task.taskId)) {
            is V25Result.Success -> {
                _pdfTask.value = result.value
                onStarted()
                pollTask(result.value)
            }
            is V25Result.Failure -> {
                handleFailure("start_task", result, surface = false)
                onFailure(result.code)
            }
        }
    }

    fun refreshPdfTask() = viewModelScope.launch {
        val task = _pdfTask.value ?: return@launch
        when (val result = v25Repository.getTask(task.taskId)) {
            is V25Result.Success -> {
                _pdfTask.value = result.value
                if (result.value.status == V25TaskStatus.COMPLETED) {
                    refreshDecks()
                    refreshDashboard()
                    refreshTodayPlan()
                }
            }
            is V25Result.Failure -> handleFailure("get_task", result)
        }
    }

    fun abandonPdfTask(onDone: () -> Unit = {}) = viewModelScope.launch {
        val task = _pdfTask.value ?: return@launch
        when (val result = v25Repository.abandonTask(task.taskId)) {
            is V25Result.Success -> {
                _pdfTask.value = result.value
                onDone()
            }
            is V25Result.Failure -> handleFailure("abandon_task", result)
        }
    }

    fun retryPdfTask(onReady: () -> Unit = {}) = viewModelScope.launch {
        val task = _pdfTask.value ?: return@launch
        when (val result = v25Repository.retryTask(task.taskId)) {
            is V25Result.Success -> {
                _pdfTask.value = result.value
                _pdfSamples.value = result.value.sampleCards.map { CardDraft(it.front, it.back) }
                onReady()
            }
            is V25Result.Failure -> handleFailure("retry_task", result)
        }
    }

    /** Wait for the server-owned sample worker; never infer samples from local UI configuration. */
    private suspend fun awaitSampleCards(initial: V25GenerationTask): V25Result<List<V25SampleCard>> {
        var current = initial
        repeat(120) {
            when (current.status) {
                V25TaskStatus.AWAITING_SAMPLE_CONFIRMATION -> {
                    _pdfTask.value = current
                    return V25Result.Success(current.sampleCards)
                }
                V25TaskStatus.FAILED, V25TaskStatus.ABANDONED -> {
                    _pdfTask.value = current
                    return V25Result.Failure(current.errorCode ?: V25ErrorCodes.GENERATION_FAILED)
                }
                else -> Unit
            }
            delay(2_500)
            when (val result = v25Repository.getTask(current.taskId)) {
                is V25Result.Success -> {
                    current = result.value
                    // Keep the persisted task state visible while the worker is running. This
                    // makes a screen re-entry observe SAMPLE_GENERATING instead of the stale
                    // DRAFT snapshot and gives the user an honest progress state.
                    _pdfTask.value = current
                }
                is V25Result.Failure -> return result
            }
        }
        return V25Result.Failure("SAMPLE_TIMEOUT")
    }

    /** A generated sample already owns a DRAFT task; starting it is the only valid next action. */
    fun createPdfTask(
        @Suppress("UNUSED_PARAMETER") existingDeckId: String?,
        @Suppress("UNUSED_PARAMETER") deckName: String,
        @Suppress("UNUSED_PARAMETER") chapterIds: List<String>,
        @Suppress("UNUSED_PARAMETER") config: PdfGenerationConfig,
        onStarted: () -> Unit,
        onFailure: (String?) -> Unit = {},
    ) = startPdfTask(onStarted, onFailure)

    /** V2.5 has no pause/resume task state; refresh is the only safe re-entry operation. */
    fun resumePdfTask() {
        refreshPdfTask()
    }

    fun beginTextImportFlow(deckName: String, cards: List<CardDraft>) {
        _textImportFlow.value = TextImportFlow(deckName, cards)
    }

    fun clearTextImportFlow() {
        _textImportFlow.value = null
    }

    fun startStudy(deckId: String, reviewMode: Boolean) = viewModelScope.launch {
        val result = if (reviewMode) {
            v25Repository.deckReviewQueue(deckId).mapSuccess { queue -> queue.map { it.card } }
        } else {
            v25Repository.listCards(deckId, V25BrowseFilter(V25BrowseOrder.random, mastery = V25MasteryFilter.all))
        }
        when (result) {
            is V25Result.Success -> _studyCards.value = result.value.map(::toFlashcard)
            is V25Result.Failure -> handleFailure("load_study", result)
        }
    }

    /** Loads the server-computed core queue for the single today-study route. */
    fun startTodayStudy() = viewModelScope.launch {
        // Never render a previous deck/today queue while the server computes the current plan.
        _studyCards.value = emptyList()
        when (val result = v25Repository.todayPlan()) {
            is V25Result.Success -> {
                setTodayPlan(result.value)
                _studyCards.value = result.value.cards.map { toFlashcard(it.card) }
            }
            is V25Result.Failure -> handleFailure("load_today_study", result)
        }
    }

    /** Loads the optional overflow queue after the core daily review target is complete. */
    fun startTodayBacklogStudy(onLoaded: (Boolean) -> Unit = {}) = viewModelScope.launch {
        // Do not let a failed backlog request repopulate the just-completed core queue through
        // StudyScreen's cards observer.  The caller will keep the completion state visible until
        // a fresh server response arrives.
        _studyCards.value = emptyList()
        val succeeded = when (val result = v25Repository.studyPlanBacklog()) {
            is V25Result.Success -> {
                _studyCards.value = result.value.map { toFlashcard(it.card) }
                true
            }
            is V25Result.Failure -> {
                handleFailure("load_today_backlog", result)
                false
            }
        }
        onLoaded(succeeded)
    }

    /**
     * Submits one user rating through the local outbox. The swipe transaction commits the
     * event (fixed client_event_id + Idempotency-Key) and hides the card from the local queue,
     * so [onSuccess] (queue advance) fires as soon as the local write is durable — never after
     * a network round-trip. The sync coordinator replays the event and the merged refresh
     * (decks / today plan / dashboard, once per drained pass) updates the Room projections;
     * there is deliberately no per-rating multi-endpoint fan-out anymore.
     */
    fun rate(cardId: String, rating: Rating, onSuccess: () -> Unit = {}) = viewModelScope.launch {
        when (val result = reviewCoordinator.submit(cardId, V25Rating.valueOf(rating.name))) {
            is V25Result.Success -> onSuccess()
            is V25Result.Failure -> {
                if (result.code != ReviewCoordinator.IN_FLIGHT_CODE) handleFailure("submit_review", result)
            }
        }
    }

    fun refreshCards(deckId: String): Job = viewModelScope.launch {
        when (val result = v25Repository.listCards(deckId, V25BrowseFilter(V25BrowseOrder.position))) {
            is V25Result.Success -> cardFlow(deckId).value = result.value.map(::toFlashcard)
            is V25Result.Failure -> handleFailure("list_cards", result)
        }
    }

    fun cards(deckId: String): Flow<List<FlashcardEntity>> = cardFlow(deckId).asStateFlow()

    fun deckProgress(deckId: String): Flow<DeckProgress> = _decks.map { values ->
        values.firstOrNull { it.id == deckId }?.let {
            DeckProgress(it.cardCount, it.dueCount, it.masteredCards, it.reviewCount)
        } ?: DeckProgress(0, 0, 0, 0)
    }

    fun importDeck(
        name: String,
        drafts: List<CardDraft>,
        onFailure: () -> Unit = {},
        onDone: (String) -> Unit,
    ) = viewModelScope.launch {
        val valid = drafts.filter { it.front.isNotBlank() && it.back.isNotBlank() }
        if (name.isBlank() || valid.isEmpty()) return@launch
        when (val result = importCoordinator.submit(
            ImportTarget.NewDeck(name.trim()),
            valid.map { V25CardDraft(it.front.trim(), it.back.trim()) },
        )) {
            // Navigate and clear drafts only after the server committed the batch.
            is V25Result.Success -> {
                refreshDecks()
                refreshTodayPlan()
                onDone(result.value)
            }
            is V25Result.Failure -> {
                if (result.code != ImportCoordinator.IN_FLIGHT_CODE) {
                    handleFailure("import_deck", result)
                    onFailure()
                }
            }
        }
    }

    /** Visual theme is local presentation only; deck persistence remains server-authoritative. */
    fun importDeck(
        name: String,
        drafts: List<CardDraft>,
        onDone: (String) -> Unit,
        @Suppress("UNUSED_PARAMETER") themeKey: String,
    ) = importDeck(name, drafts, onDone = onDone)

    fun addCardsToDeck(
        deckId: String,
        drafts: List<CardDraft>,
        onFailure: () -> Unit = {},
        onDone: () -> Unit,
    ) = viewModelScope.launch {
        val valid = drafts.filter { it.front.isNotBlank() && it.back.isNotBlank() }
        if (valid.isEmpty()) return@launch
        when (val result = importCoordinator.submit(
            ImportTarget.ExistingDeck(deckId),
            valid.map { V25CardDraft(it.front.trim(), it.back.trim()) },
        )) {
            is V25Result.Success -> {
                refreshCards(deckId)
                refreshDecks()
                onDone()
            }
            is V25Result.Failure -> {
                if (result.code != ImportCoordinator.IN_FLIGHT_CODE) {
                    handleFailure("add_cards", result)
                    onFailure()
                }
            }
        }
    }

    fun updateDeckName(deckId: String, name: String, onFailure: () -> Unit = {}) = viewModelScope.launch {
        when (val result = v25Repository.renameDeck(deckId, name.trim())) {
            is V25Result.Success -> refreshDecks()
            is V25Result.Failure -> {
                handleFailure("rename_deck", result)
                onFailure()
            }
        }
    }

    fun deleteDeck(
        deckId: String,
        onSuccess: () -> Unit = {},
        onFailure: () -> Unit = {},
        abandonPreGenerationTasks: Boolean = false,
        cancelActiveTasks: Boolean = false,
    ) = viewModelScope.launch {
        val operation = deckDeletionOperationKey(
            deckId,
            abandonPreGenerationTasks,
            cancelActiveTasks,
        )
        val idempotencyKey = beginWrite(operation)
        if (idempotencyKey == null) {
            onFailure()
            return@launch
        }
        var succeeded = false
        try {
            when (
                val result = v25Repository.deleteDeck(
                    deckId,
                    abandonPreGenerationTasks,
                    idempotencyKey,
                    cancelActiveTasks,
                )
            ) {
                is V25Result.Success -> {
                    succeeded = true
                    cardFlows.remove(deckId)
                    _deletionPreflights.value = _deletionPreflights.value
                        .filterKeys { it != operation }
                    refreshDecks()
                    onSuccess()
                }
                is V25Result.Failure -> {
                    handleFailure("delete_deck", result)
                    onFailure()
                }
            }
        } finally {
            finishWrite(operation, succeeded)
        }
    }

    fun updateCard(card: FlashcardEntity, onFailure: () -> Unit = {}) = viewModelScope.launch {
        when (val result = v25Repository.updateCard(card.id, card.front.trim(), card.back.trim())) {
            is V25Result.Success -> refreshCards(card.deckId)
            is V25Result.Failure -> {
                handleFailure("update_card", result)
                onFailure()
            }
        }
    }

    fun deleteCard(card: FlashcardEntity, onFailure: () -> Unit = {}) = viewModelScope.launch {
        when (val result = v25Repository.deleteCard(card.id)) {
            is V25Result.Success -> {
                cardFlow(card.deckId).value = cardFlow(card.deckId).value.filterNot { it.id == card.id }
                armDeletionUndo(result.value)
                refreshDecks()
            }
            is V25Result.Failure -> {
                handleFailure("delete_card", result)
                onFailure()
            }
        }
    }

    fun undoLastDeletion(onFinished: () -> Unit = {}) = viewModelScope.launch {
        val pending = _pendingDeletion.value ?: return@launch
        when (val result = v25Repository.undoDeletionBatch(pending.deleteBatchId)) {
            is V25Result.Success -> {
                _pendingDeletion.value = null
                cardFlows.keys.toList().forEach(::refreshCards)
                refreshDecks()
                onFinished()
            }
            is V25Result.Failure -> handleFailure("undo_deletion", result)
        }
    }

    fun refreshApiKeyStatus(): Job = viewModelScope.launch {
        when (val result = v25Repository.apiKeyStatus()) {
            is V25Result.Success -> _apiKeyStatus.value = result.value.toLegacyApiKeyStatus()
            is V25Result.Failure -> handleFailure("api_key_status", result)
        }
    }

    fun saveApiKey(key: String, onFinished: () -> Unit = {}) = viewModelScope.launch {
        when (val result = v25Repository.saveApiKey(key)) {
            is V25Result.Success -> _apiKeyStatus.value = result.value.toLegacyApiKeyStatus()
            is V25Result.Failure -> handleFailure("save_api_key", result)
        }
        onFinished()
    }

    fun checkApiKeyForGeneration(
        onAvailable: () -> Unit,
        onUnavailable: (String) -> Unit,
        onFailure: () -> Unit,
    ) = viewModelScope.launch {
        when (val result = v25Repository.apiKeyStatus()) {
            is V25Result.Success -> {
                _apiKeyStatus.value = result.value.toLegacyApiKeyStatus()
                if (result.value.state == V25ApiKeyState.AVAILABLE) onAvailable() else onUnavailable(result.value.state.name)
            }
            is V25Result.Failure -> {
                handleFailure("api_key_for_generation", result)
                onFailure()
            }
        }
    }

    private fun setDashboard(value: V25StatsDashboard) {
        _dashboard.value = DashboardUiState(
            hasData = value.hasData,
            weeklyGoal = value.weeklyGoal,
            completed = value.weeklyGoalCompleted,
            weeklyGoalRate = value.weeklyGoalRate,
            recallAccuracy = value.recallAccuracy,
            firstAttemptAccuracy = value.firstAttemptAccuracy,
            retentionRate = value.retentionRate,
            streakDays = value.streakDays,
            masteredCards = value.masteredCards,
        )
        _weeklyActivity.value = WeeklyActivityData(
            dailyCounts = value.weeklyActivity.map { it.ratingCount }.padTo(7),
            total = value.weeklyTotalRatings,
            changePercent = value.weeklyChangeRate?.times(100)?.toInt(),
        )
    }

    private fun setTodayPlan(value: V25TodayPlan) {
        _todayPlan.value = TodayPlanUiState(
            dailyGoal = value.dailyGoal,
            completedCount = value.completedCount,
            dueCount = value.dueCount,
            remainingCount = value.planRemaining,
            planConfigured = value.planConfigured,
            selectedDeckIds = value.selectedDeckIds,
            dailyNewGoal = value.dailyNewGoal,
            dailyReviewGoal = value.dailyReviewGoal,
            newCompletedCount = value.newCompletedCount,
            reviewCompletedCount = value.reviewCompletedCount,
            newRemainingCount = value.newRemainingCount,
            reviewRemainingCount = value.reviewRemainingCount,
            coreTargetCount = value.coreTargetCount,
            backlogCount = value.backlogCount,
        )
    }

    private fun setStudyPlan(value: V25StudyPlan) {
        _studyPlan.value = StudyPlanUiState(
            loaded = true,
            saving = _studyPlan.value.saving,
            configured = value.configured,
            currentProjectId = value.currentProjectId,
            selectedDeckIds = value.selectedDeckIds,
            dailyNewGoal = value.dailyNewGoal,
            dailyReviewGoal = value.dailyReviewGoal,
        )
    }

    private fun recoverPendingDeletion() = viewModelScope.launch {
        when (val result = v25Repository.pendingDeletionBatches()) {
            is V25Result.Success -> result.value
                .firstOrNull { it.undoUntil.isAfter(Instant.now()) }
                ?.let(::armDeletionUndo)
            is V25Result.Failure -> Unit
        }
    }

    private fun armDeletionUndo(batch: V25CardDeletionBatch) {
        val state = PendingDeletionUiState(batch.deleteBatchId, batch.undoUntil)
        _pendingDeletion.value = state
        _uiMessage.value = "已删除，可在 10 秒内撤销"
        viewModelScope.launch {
            val remaining = Duration.between(Instant.now(), state.undoUntil).toMillis().coerceAtLeast(0)
            delay(remaining)
            if (_pendingDeletion.value?.deleteBatchId == state.deleteBatchId) _pendingDeletion.value = null
        }
    }

    private suspend fun pollTask(initial: V25GenerationTask) {
        var current = initial
        repeat(240) {
            when (current.status) {
                V25TaskStatus.COMPLETED -> {
                    refreshDecks()
                    refreshDashboard()
                    refreshTodayPlan()
                    return
                }
                V25TaskStatus.FAILED, V25TaskStatus.ABANDONED -> return
                else -> Unit
            }
            delay(2_500)
            when (val result = v25Repository.getTask(current.taskId)) {
                is V25Result.Success -> {
                    current = result.value
                    _pdfTask.value = current
                }
                is V25Result.Failure -> {
                    handleFailure("get_task", result)
                    return
                }
            }
        }
        _uiMessage.value = "生成状态暂时无法更新，请稍后在项目中查看。"
    }

    private fun cardFlow(deckId: String): MutableStateFlow<List<FlashcardEntity>> =
        cardFlows.getOrPut(deckId) { MutableStateFlow(emptyList()) }

    private fun projectDeletionKey(projectId: String, retainDecks: Boolean?): String =
        if (retainDecks == null) "delete_project:$projectId"
        else "project:$projectId:retain=$retainDecks"

    private fun deckDeletionKey(deckId: String): String = "delete_deck:$deckId"

    private fun projectDeletionOperationKey(
        projectId: String,
        retainDecks: Boolean,
        abandonPreGenerationTasks: Boolean,
        cancelActiveTasks: Boolean,
    ): String =
        "delete_project:$projectId:retain=$retainDecks:abandon=$abandonPreGenerationTasks:cancel=$cancelActiveTasks"

    private fun deckDeletionOperationKey(
        deckId: String,
        abandonPreGenerationTasks: Boolean,
        cancelActiveTasks: Boolean,
    ): String =
        "delete_deck:$deckId:abandon=$abandonPreGenerationTasks:cancel=$cancelActiveTasks"

    /** One semantic destructive action gets one stable idempotency key and one in-flight gate. */
    private fun beginWrite(operation: String): String? {
        if (!inFlightWrites.add(operation)) return null
        val key = writeKeys.getOrPut(operation) { UUID.randomUUID().toString() }
        _deletionInFlight.value = inFlightWrites.toSet()
        return key
    }

    private fun finishWrite(operation: String, succeeded: Boolean) {
        inFlightWrites.remove(operation)
        if (succeeded) writeKeys.remove(operation)
        _deletionInFlight.value = inFlightWrites.toSet()
    }

    private fun clearAuthenticatedState() {
        _projects.value = emptyList()
        projectsById.clear()
        _projectTasks.value = emptyMap()
        _projectProgress.value = emptyMap()
        _deletionPreflights.value = emptyMap()
        taskRefreshGeneration.clear()
        inFlightWrites.clear()
        writeKeys.clear()
        _deletionInFlight.value = emptySet()
        _decks.value = emptyList()
        _studyCards.value = emptyList()
        _dashboard.value = null
        _weeklyActivity.value = WeeklyActivityData()
        _todayPlan.value = TodayPlanUiState()
        _studyPlan.value = StudyPlanUiState()
        studyPlanIdempotencyKey = null
        studyPlanRequestFingerprint = null
        _apiKeyStatus.value = null
        cardFlows.clear()
        clearPdfFlow()
        _projectCreationMaterials.value = emptyList()
        _projectMaterials.value = emptyMap()
        _materialImportDrafts.value = emptyList()
        _textImportFlow.value = null
        _projectGenerationDraft.value = null
        _pendingDeletion.value = null
        importCoordinator.reset()
        reviewCoordinator.reset()
        pdfUploadCoordinator.reset()
    }

    private fun clearPdfFlow() {
        _activePdfProject.value = null
        _pdfFile.value = null
        _pdfSamples.value = emptyList()
        _pdfTask.value = null
        _pdfTaskDeckId.value = null
    }

    private fun handleFailure(operation: String, result: V25Result.Failure, surface: Boolean = true) {
        if (result.isAuthFailure) {
            sessionStore.clear()
            auth.checkSession()
        }
        if (surface) _uiMessage.value = userMessage(result)
        if (BuildConfig.DEBUG) Log.w("ShankaNetwork", "op=$operation code=${result.code}")
    }

    private fun userMessage(result: V25Result.Failure): String =
        if (result.code == "NETWORK_UNAVAILABLE") "网络错误，请稍后重试" else ErrorMessages.forCode(result.code)

    private fun toProjectSummary(project: V25LearningProject) = com.qiuzhao.flashcards.data.remote.ProjectSummary(
        id = project.projectId,
        name = project.name,
        themeKey = themeFor(project.projectId),
        deckCount = project.deckCount,
        materialCount = 1,
    )

    private fun syncProjectMaterials(projects: List<V25LearningProject>) {
        _projectMaterials.value = projects.associate { project ->
            project.projectId to listOf(project.toProjectMaterial())
        }
    }

    private fun syncProjectMaterial(project: V25LearningProject) {
        _projectMaterials.value = _projectMaterials.value.toMutableMap().apply {
            put(project.projectId, listOf(project.toProjectMaterial()))
        }
    }

    private fun V25LearningProject.toProjectMaterial() = ProjectDraftMaterial(
        id = "project-pdf-${projectId}-${file.id}",
        type = ProjectDraftMaterialType.FILE,
        title = file.name,
        extension = file.name.substringAfterLast('.', "").lowercase().ifBlank { "pdf" },
        importedAt = file.createdAt,
    )

    private fun toDeckSummary(deck: V25Deck) = DeckSummary(
        id = deck.deckId,
        name = deck.name,
        source = "V25",
        themeKey = themeFor(deck.projectId ?: deck.deckId),
        cardCount = deck.cardCount,
        dueCount = deck.dueCount,
        masteredCards = deck.masteredCards,
        reviewCount = deck.reviewCount,
        masteryRatio = deck.masteryRatio,
        notStartedCount = deck.notStartedCount,
        learningCount = deck.learningCount,
        relearningCount = deck.relearningCount,
        consolidatingCount = deck.consolidatingCount,
        masteredLifecycleCount = deck.masteredCount,
        reviewEventCount = deck.reviewEventCount,
        lastStudiedAt = deck.lastStudiedAt?.toString(),
        projectId = deck.projectId,
    )

    private fun toFlashcard(card: V25Card) = FlashcardEntity(
        id = card.cardId,
        deckId = card.deckId,
        front = card.front,
        back = card.back,
        position = card.position,
        source = "V25",
        version = card.version,
    )

    private fun V25LearningProject.toPdfFile() = PdfFile(
        id = file.id,
        name = file.name,
        status = status.name,
        chapters = file.chapters.map { PdfChapter(it.id, it.name, it.startPage, it.endPage) },
    )

    private fun com.qiuzhao.flashcards.domain.v25.V25ApiKeyStatus.toLegacyApiKeyStatus() = ApiKeyStatus(
        status = state.name,
        maskedKey = maskedKey.orEmpty(),
    )

    private fun coverageMode(quantity: String): V25CoverageMode = when (quantity.uppercase()) {
        "COMPACT", "精简" -> V25CoverageMode.COMPACT
        "EXTENSIVE", "充分" -> V25CoverageMode.EXTENSIVE
        else -> V25CoverageMode.BALANCED
    }

    private fun difficultyRatio(config: PdfGenerationConfig): V25DifficultyRatio {
        val basic = ((config.basic * 100).toInt().coerceIn(0, 100) / 10) * 10
        val understanding = ((config.understanding * 100).toInt().coerceIn(0, 100) / 10) * 10
        val deep = 100 - basic - understanding
        return if (deep in 0..100 && deep % 10 == 0) {
            V25DifficultyRatio(basic, understanding, deep)
        } else {
            V25DifficultyRatio(40, 40, 20)
        }
    }

    private fun themeFor(id: String): String = projectThemes[Math.floorMod(id.hashCode(), projectThemes.size)]

    private fun android.content.ContentResolver.displayName(uri: Uri): String =
        query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use { cursor ->
            val index = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
            if (index >= 0 && cursor.moveToFirst()) cursor.getString(index) else null
        }.orEmpty().ifBlank { uri.lastPathSegment?.substringAfterLast('/') ?: "learning-project.pdf" }

    private val contentResolver: android.content.ContentResolver
        get() = getApplication<Application>().contentResolver

    private fun <T, R> V25Result<T>.mapSuccess(transform: (T) -> R): V25Result<R> = when (this) {
        is V25Result.Success -> V25Result.Success(transform(value))
        is V25Result.Failure -> this
    }

    private fun List<Int>.padTo(size: Int): List<Int> =
        if (this.size >= size) take(size) else this + List(size - this.size) { 0 }
}
