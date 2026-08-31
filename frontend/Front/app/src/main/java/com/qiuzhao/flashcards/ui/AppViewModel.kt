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
import com.qiuzhao.flashcards.domain.v25.V25MaterialStatus
import com.qiuzhao.flashcards.domain.v25.V25MaterialType
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
import kotlinx.coroutines.isActive
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
 * Presentation-only state for the project/material screens. In the creation wizard a material
 * is staged before any server call exists (two-step creation: the project is created first,
 * then every staged material uploads through the materials endpoints). Server-backed materials
 * carry their real ids and statuses so the management screens render the contract states.
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
    /** Owning project for server-backed materials; null for creation-flow local drafts. */
    val projectId: String? = null,
    /** Server material id once the material exists on the backend; null for local drafts. */
    val materialId: String? = null,
    /** Wire material status (PENDING/PARSING/PARSED/FAILED/READY); null for local drafts. */
    val serverStatus: String? = null,
    /** TEXT-only character count from the server. */
    val charCount: Int? = null,
    /** PDF parse failure code from the server. */
    val errorCode: String? = null,
)

private fun ProjectDraftMaterial.renamedFile(rawTitle: String): ProjectDraftMaterial {
    val normalized = rawTitle.trim().ifBlank { title }
    val resolvedExtension = normalized.substringAfterLast('.', extension.orEmpty()).lowercase()
    return copy(
        title = normalized,
        extension = resolvedExtension.ifBlank { extension.orEmpty() }.ifBlank { null },
    )
}

/** Coarse phase of the parse wait; the chapter screen renders from this, not a private loop. */
internal enum class ParseWaitPhase { IDLE, POLLING, FAILED, UNRESOLVED }

/** One FAILED PDF material behind a parse failure; the failure card replaces it in place. */
internal data class FailedParseMaterial(
    val materialId: String,
    val materialName: String,
    val errorCode: String?,
)

/**
 * Server-driven parse wait for the smart-card flow. [FAILED] carries every FAILED PDF material
 * of the project (contract: per-material replace) plus the first backend `error_code`
 * (PDF_PARSE_FAILED / PDF_TOC_MISSING / …); [UNRESOLVED] means the wait window ended (timeout or
 * repeated network failures) without a server verdict.
 */
internal data class ParseWaitUiState(
    val projectId: String? = null,
    val phase: ParseWaitPhase = ParseWaitPhase.IDLE,
    val errorCode: String? = null,
    val reason: String? = null,
    val failedMaterials: List<FailedParseMaterial> = emptyList(),
)

/** Ephemeral handoff from a text parser into the existing deck-import flow. */
data class TextImportFlow(val deckName: String, val cards: List<CardDraft>)

// Parse-wait poller cadence: fast at first (a parse usually finishes within seconds), then
// backing off so a long tail never hammers the tunnel link; the window itself is bounded.
private const val PARSE_POLL_INITIAL_DELAY_MS = 1_000L
private const val PARSE_POLL_MAX_DELAY_MS = 5_000L
private const val PARSE_POLL_TIMEOUT_MS = 180_000L
private const val PARSE_POLL_MAX_CONSECUTIVE_FAILURES = 5

/** TextMaterialCreateRequest cap (openapi: content ≤30000 trimmed characters). */
private const val MAX_TEXT_MATERIAL_CHARS = 30_000

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
    private val _parseWait = MutableStateFlow(ParseWaitUiState())
    internal val parseWait: StateFlow<ParseWaitUiState> = _parseWait.asStateFlow()
    private var parsePollJob: Job? = null
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

    /**
     * Two-step project creation (contract V25-D-29): one JSON POST /projects, then one materials
     * call per staged material; retries replay only the failed step with fixed keys.
     */
    private val projectCreationCoordinator = ProjectCreationCoordinator(v25Repository)
    val projectCreationAttempt: StateFlow<ProjectCreationAttempt?> = projectCreationCoordinator.attempt
    val projectCreating: StateFlow<Boolean> = projectCreationCoordinator.creating

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

    fun refreshProjects(forceRefresh: Boolean = false): Job = viewModelScope.launch {
        when (val result = v25Repository.listProjects(forceRefresh)) {
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

    /** Starts a new project form; the wizard stages any number of materials before the upload. */
    fun resetProjectCreationDraft() {
        _projectCreationMaterials.value = emptyList()
        _materialImportDrafts.value = emptyList()
        projectCreationCoordinator.reset()
    }

    /**
     * Stores a URI only until the user confirms project creation.  Keeping the URI instead of
     * just a display name is essential: the actual PDF bytes can only be read at upload time
     * through the ContentResolver.
     */
    fun addProjectDraftFile(uri: Uri, displayName: String = contentResolver.displayName(uri)) {
        val extension = displayName.substringAfterLast('.', "").lowercase()
        if (extension != "pdf") {
            _uiMessage.value = "仅支持 PDF 文件"
            return
        }
        if (_projectCreationMaterials.value.any { it.uri == uri }) return
        _projectCreationMaterials.value = _projectCreationMaterials.value + ProjectDraftMaterial(
            id = "project-pdf-${System.nanoTime()}",
            type = ProjectDraftMaterialType.FILE,
            title = displayName,
            extension = extension,
            uri = uri,
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
            if (material.type == ProjectDraftMaterialType.FILE && material.id == materialId) {
                material.renamedFile(title)
            } else if (material.id == materialId) {
                material.copy(title = title.trim().ifBlank { material.title })
            } else {
                material
            }
        }
    }

    /** Stages a creation-flow text draft; the editor commits through [upsertProjectDraftText]. */
    internal fun stageProjectDraftText(): String {
        val id = "project-text-${System.nanoTime()}"
        _projectCreationMaterials.value = _projectCreationMaterials.value + ProjectDraftMaterial(
            id = id,
            type = ProjectDraftMaterialType.TEXT,
            title = "",
        )
        return id
    }

    /** Staged creation texts land as local drafts; they upload with the project creation. */
    internal fun upsertProjectDraftText(materialId: String?, title: String, content: String) {
        val normalizedTitle = title.trim()
        if (normalizedTitle.isBlank()) {
            _uiMessage.value = "请填写资料标题"
            return
        }
        val error = textContentError(content)
        if (error != null) {
            _uiMessage.value = error
            return
        }
        val staged = materialId ?: "project-text-${System.nanoTime()}"
        val draft = ProjectDraftMaterial(
            id = staged,
            type = ProjectDraftMaterialType.TEXT,
            title = normalizedTitle,
            content = content.trim(),
        )
        _projectCreationMaterials.value =
            _projectCreationMaterials.value.filterNot { it.id == staged } + draft
    }

    internal fun projectMaterialList(projectId: String): List<ProjectDraftMaterial> =
        _projectMaterials.value[projectId].orEmpty()

    /** Renames a creation-flow draft text in place. */
    internal fun renameProjectDraftText(materialId: String, title: String, content: String) =
        upsertProjectDraftText(materialId, title, content)

    /**
     * Commits a pasted text directly to an existing project through POST materials/text. The
     * text body is part of the idempotent operation, so its hash joins the retry identity.
     */
    internal fun addTextMaterialToProject(
        projectId: String,
        materialId: String?,
        title: String,
        content: String,
        onResult: (Boolean) -> Unit = {},
    ) {
        val normalizedTitle = title.trim()
        if (normalizedTitle.isBlank()) {
            _uiMessage.value = "请填写资料标题"
            onResult(false)
            return
        }
        textContentError(content)?.let { error ->
            _uiMessage.value = error
            onResult(false)
            return
        }
        viewModelScope.launch {
            val normalized = content.trim()
            val operation = "add_material_text:$projectId:${normalized.hashCode()}"
            val key = beginWrite(operation)
            if (key == null) {
                onResult(false)
                return@launch
            }
            var succeeded = false
            try {
                when (val result = v25Repository.addProjectMaterialText(projectId, normalizedTitle, normalized, key)) {
                    is V25Result.Success -> {
                        succeeded = true
                        refreshProjects()
                        onResult(true)
                    }
                    is V25Result.Failure -> {
                        handleFailure("add_material_text", result)
                        onResult(false)
                    }
                }
            } finally {
                finishWrite(operation, succeeded)
            }
        }
    }

    /** Client-side contract check mirroring the server's 1..30000 trimmed-character rule. */
    private fun textContentError(content: String): String? = when {
        content.isBlank() -> "文本内容不能为空"
        content.trim().length > MAX_TEXT_MATERIAL_CHARS -> "文本内容最多 $MAX_TEXT_MATERIAL_CHARS 字"
        else -> null
    }

    /** Server material names are server-owned titles; no material-rename endpoint exists. */
    internal fun renameProjectFile(
        @Suppress("UNUSED_PARAMETER") materialId: String,
        @Suppress("UNUSED_PARAMETER") title: String,
    ) {
        _uiMessage.value = "资料名称由服务端管理，暂不支持修改"
    }

    internal fun beginMaterialImport() {
        _materialImportDrafts.value = emptyList()
    }

    internal fun stageMaterialImportFiles(uris: List<Uri>) {
        for (uri in uris) {
            val displayName = contentResolver.displayName(uri)
            val extension = displayName.substringAfterLast('.', "").lowercase()
            if (extension != "pdf") {
                _uiMessage.value = "仅支持 PDF 文件"
                continue
            }
            if (_materialImportDrafts.value.any { it.uri == uri }) continue
            _materialImportDrafts.value = _materialImportDrafts.value + ProjectDraftMaterial(
                id = "staged-pdf-${System.nanoTime()}",
                type = ProjectDraftMaterialType.FILE,
                title = displayName,
                extension = extension,
                uri = uri,
            )
        }
    }

    /** Opens the shared text editor for one new import draft; the editor commits the content. */
    internal fun stageMaterialImportText(): String {
        val id = "staged-text-${System.nanoTime()}"
        _materialImportDrafts.value = _materialImportDrafts.value + ProjectDraftMaterial(
            id = id,
            type = ProjectDraftMaterialType.TEXT,
            title = "",
        )
        return id
    }

    /** Editor callback for an import-flow text draft (staged until "识别并导入"). */
    internal fun upsertMaterialImportText(materialId: String?, title: String, content: String) {
        val normalizedTitle = title.trim()
        if (normalizedTitle.isBlank()) {
            _uiMessage.value = "请填写资料标题"
            return
        }
        textContentError(content)?.let { error ->
            _uiMessage.value = error
            return
        }
        val staged = materialId ?: "staged-text-${System.nanoTime()}"
        val draft = ProjectDraftMaterial(
            id = staged,
            type = ProjectDraftMaterialType.TEXT,
            title = normalizedTitle,
            content = content.trim(),
        )
        _materialImportDrafts.value =
            _materialImportDrafts.value.filterNot { it.id == staged } + draft
    }

    internal fun removeMaterialImportDraft(materialId: String) {
        _materialImportDrafts.value = _materialImportDrafts.value.filterNot { it.id == materialId }
    }

    internal fun renameMaterialImportFile(materialId: String, title: String) {
        _materialImportDrafts.value = _materialImportDrafts.value.map { material ->
            if (material.type == ProjectDraftMaterialType.FILE && material.id == materialId) {
                material.renamedFile(title)
            } else if (material.id == materialId) {
                material.copy(title = title.trim().ifBlank { material.title })
            } else {
                material
            }
        }
    }

    /**
     * Commits the staged import. For the creation flow (projectId == null) the drafts join the
     * wizard and upload with the project; for a living project each draft lands immediately
     * through its materials endpoint (PDF parse asynchronously, text READY in-line).
     */
    internal fun commitMaterialImport(
        projectId: String?,
        onResult: (success: Boolean, message: String?) -> Unit = { _, _ -> },
    ) {
        val staged = _materialImportDrafts.value
        if (staged.isEmpty()) {
            val message = "请先添加资料"
            _uiMessage.value = message
            onResult(false, message)
            return
        }
        val badText = staged.firstOrNull { it.type == ProjectDraftMaterialType.TEXT && textContentError(it.content) != null }
        if (badText != null) {
            val message = textContentError(badText.content) ?: "文本资料内容无效"
            _uiMessage.value = message
            onResult(false, message)
            return
        }
        if (projectId == null) {
            _projectCreationMaterials.value = staged
            _materialImportDrafts.value = emptyList()
            onResult(true, null)
            return
        }
        commitStagedMaterials(projectId, staged) { success, message ->
            if (success) _materialImportDrafts.value = emptyList()
            else _uiMessage.value = message ?: "资料上传失败"
            onResult(success, message)
        }
    }

    /** Uploads every staged draft to a living project; one failed material stops the commit. */
    private fun commitStagedMaterials(
        projectId: String,
        staged: List<ProjectDraftMaterial>,
        onResult: (success: Boolean, message: String?) -> Unit,
    ) {
        viewModelScope.launch {
            for (material in staged) {
                val outcome = when (material.type) {
                    ProjectDraftMaterialType.FILE -> commitStagedPdf(projectId, material)
                    ProjectDraftMaterialType.TEXT -> {
                        val operation = "add_material_text:$projectId:${material.content.hashCode()}"
                        val key = beginWrite(operation)
                        if (key == null) {
                            V25Result.Failure(ImportCoordinator.IN_FLIGHT_CODE, null, null)
                        } else {
                            var succeeded = false
                            try {
                                val result = v25Repository.addProjectMaterialText(
                                    projectId,
                                    material.title,
                                    material.content,
                                    key,
                                )
                                succeeded = result is V25Result.Success
                                result
                            } finally {
                                finishWrite(operation, succeeded)
                            }
                        }
                    }
                }
                if (outcome is V25Result.Failure) {
                    if (outcome.code != ImportCoordinator.IN_FLIGHT_CODE) handleFailure("commit_material", outcome, surface = false)
                    onResult(false, userMessage(outcome))
                    return@launch
                }
            }
            refreshProjects()
            onResult(true, null)
        }
    }

    /** One staged PDF → POST materials/pdf through the upload coordinator's fixed key. */
    private suspend fun commitStagedPdf(projectId: String, material: ProjectDraftMaterial): V25Result<*> {
        val uri = material.uri
            ?: return V25Result.Failure(V25ErrorCodes.INVALID_RESPONSE, null, "无法读取所选 PDF")
        val fileName = contentResolver.displayName(uri)
        val attempt = pdfUploadCoordinator.begin(PdfUploadOperation.AddMaterial(projectId), uri.toString(), fileName)
            ?: return V25Result.Failure(ImportCoordinator.IN_FLIGHT_CODE, null, null)
        val input = contentResolver.openInputStream(uri)
        if (input == null) {
            pdfUploadCoordinator.fail()
            return V25Result.Failure(V25ErrorCodes.INVALID_RESPONSE, null, "无法读取所选 PDF")
        }
        return try {
            input.use { content ->
                v25Repository.addProjectMaterialPdf(projectId, fileName, content, attempt.idempotencyKey)
            }.also { result ->
                if (result is V25Result.Success) pdfUploadCoordinator.commit() else pdfUploadCoordinator.fail()
            }
        } catch (cancelled: kotlinx.coroutines.CancellationException) {
            throw cancelled
        } catch (failure: Throwable) {
            pdfUploadCoordinator.fail()
            V25Result.Failure(V25ErrorCodes.INVALID_RESPONSE, null, failure.message)
        }
    }

    /**
     * Two-step creation (contract V25-D-29): POST /projects with the JSON name, then every
     * staged material through POST materials/pdf|text. Zero staged materials is valid — the
     * project is created EMPTY and the guide screen offers the add-material entries.
     */
    fun createProjectFromDraft(
        name: String,
        @Suppress("UNUSED_PARAMETER") themeKey: String,
        onResult: (projectId: String?, error: String?) -> Unit,
    ) {
        val normalized = name.trim()
        if (normalized.isEmpty()) {
            onResult(null, "请填写项目名称")
            return
        }
        val materials = _projectCreationMaterials.value
        val uploads = materials.mapNotNull { material ->
            when (material.type) {
                ProjectDraftMaterialType.FILE -> material.uri?.let { uri ->
                    MaterialUpload.Pdf(
                        draftId = material.id,
                        materialName = material.title,
                        openStream = { contentResolver.openInputStream(uri) },
                    )
                }
                ProjectDraftMaterialType.TEXT -> MaterialUpload.Text(
                    draftId = material.id,
                    materialName = material.title,
                    content = material.content,
                )
            }
        }
        if (materials.any { it.type == ProjectDraftMaterialType.FILE && it.uri == null }) {
            onResult(null, "无法读取所选 PDF")
            return
        }
        viewModelScope.launch {
            when (val result = projectCreationCoordinator.submit(normalized, uploads)) {
                is V25Result.Success -> {
                    _projectCreationMaterials.value = emptyList()
                    _materialImportDrafts.value = emptyList()
                    refreshProjects()
                    onResult(result.value, null)
                }
                is V25Result.Failure -> {
                    if (result.code != ProjectCreationCoordinator.IN_FLIGHT_CODE) {
                        handleFailure("create_project", result, surface = false)
                    }
                    onResult(null, userMessage(result))
                }
            }
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
     * Compatibility entry point for the pre-project PDF wizard. It now runs the two-step
     * contract (JSON create + PDF material upload) and waits for the server-side parse result;
     * it never manufactures chapters from the filename or stores a local-only document.
     */
    fun uploadPdf(
        uri: Uri,
        onParsed: (List<PdfChapter>) -> Unit,
        onFailure: (PdfReadFailure) -> Unit,
    ) = viewModelScope.launch {
        val fileName = contentResolver.displayName(uri)
        val projectName = fileName.substringBeforeLast('.').trim().ifBlank { "未命名项目" }
        val uploads = listOf(
            MaterialUpload.Pdf(
                draftId = "wizard-pdf-${System.nanoTime()}",
                materialName = fileName,
                openStream = { contentResolver.openInputStream(uri) },
            ),
        )
        when (val created = projectCreationCoordinator.submit(projectName, uploads)) {
            is V25Result.Success -> {
                refreshProjects()
                awaitPdfParse(created.value, onParsed, onFailure)
            }
            is V25Result.Failure -> {
                handleFailure("create_project_for_pdf_wizard", created, surface = false)
                onFailure(PdfReadFailure("上传失败", userMessage(created)))
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
            when (val result = v25Repository.getProject(projectId, forceRefresh = true)) {
                is V25Result.Success -> {
                    projectsById[projectId] = result.value
                    syncProjectMaterial(result.value)
                    _activePdfProject.value = result.value
                    _pdfFile.value = result.value.toPdfFile()
                    when (result.value.status) {
                        V25ProjectStatus.EMPTY -> Unit
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
                                    result.value.materials.firstNotNullOfOrNull { it.errorCode }
                                        ?: "服务未能解析该 PDF，请替换文件后重试。",
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

    /** Replaces one FAILED PDF material in place (contract V25-D-30: only FAILED PDFs). */
    fun replaceProjectMaterial(
        projectId: String,
        materialId: String,
        uri: Uri,
        onResult: (Boolean, String?) -> Unit,
    ) = viewModelScope.launch {
        val fileName = contentResolver.displayName(uri)
        val attempt = pdfUploadCoordinator.begin(
            PdfUploadOperation.ReplaceMaterial(projectId, materialId),
            uri.toString(),
            fileName,
        )
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
            when (val result = v25Repository.replaceProjectMaterialPdf(projectId, materialId, fileName, content, attempt.idempotencyKey)) {
                is V25Result.Success -> {
                    pdfUploadCoordinator.commit()
                    refreshActiveProject(projectId)
                    refreshProjects()
                    onResult(true, null)
                }
                is V25Result.Failure -> {
                    pdfUploadCoordinator.fail()
                    handleFailure("replace_project_material", result, surface = false)
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
    ) = viewModelScope.launch {
        val operation = "delete_project:$projectId:retain=$retainDecks"
        val idempotencyKey = beginWrite(operation)
        if (idempotencyKey == null) {
            onResult(false)
            return@launch
        }
        var succeeded = false
        try {
            when (val result = v25Repository.deleteProject(projectId, retainDecks, idempotencyKey)) {
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

    /**
     * Deletes one material (contract V25-D-30). `retainCards` is the user's three-tier choice:
     * true keeps the material's generated cards, false deletes them with their review records.
     * The server silently cancels tasks referencing the material, so the project and task
     * projections refresh after a successful delete.
     */
    fun deleteMaterial(
        projectId: String,
        materialId: String,
        retainCards: Boolean,
        onResult: (Boolean) -> Unit = {},
    ) = viewModelScope.launch {
        val operation = "delete_material:$projectId:$materialId:retain=$retainCards"
        val idempotencyKey = beginWrite(operation)
        if (idempotencyKey == null) {
            onResult(false)
            return@launch
        }
        var succeeded = false
        try {
            when (val result = v25Repository.deleteProjectMaterial(projectId, materialId, retainCards, idempotencyKey)) {
                is V25Result.Success -> {
                    succeeded = true
                    projectsById[projectId] = result.value
                    syncProjectMaterial(result.value)
                    refreshProjects()
                    refreshProjectTasks(projectId)
                    refreshDecks()
                    refreshTodayPlan()
                    onResult(true)
                }
                is V25Result.Failure -> {
                    handleFailure("delete_material", result)
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
        // Forced: entering the chapter flow must observe server truth — a cached PARSING
        // snapshot taken seconds after upload would otherwise pin the wait state.
        when (val result = v25Repository.getProject(projectId, forceRefresh = true)) {
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

    /**
     * Arms the parse-wait poller for [projectId]. Terminal projects skip polling entirely; a
     * poll whose first tick finds a terminal state exits immediately, so an open that raced a
     * finishing parse costs at most one extra GET.
     */
    fun startParsePolling(projectId: String) {
        val current = _activePdfProject.value
        if (current?.projectId == projectId && current.status != V25ProjectStatus.PARSING) {
            parsePollJob?.cancel()
            parsePollJob = null
            _parseWait.value = ParseWaitUiState()
            return
        }
        if (parsePollJob?.isActive == true && _parseWait.value.let { it.projectId == projectId && it.phase == ParseWaitPhase.POLLING }) return
        parsePollJob?.cancel()
        _parseWait.value = ParseWaitUiState(projectId = projectId, phase = ParseWaitPhase.POLLING)
        parsePollJob = viewModelScope.launch { pollProjectParse(projectId) }
    }

    /** Stops the poller and clears the wait state (project deleted / signed out / flow reset). */
    fun stopParsePolling() {
        parsePollJob?.cancel()
        parsePollJob = null
        _parseWait.value = ParseWaitUiState()
    }

    /**
     * The single parse-wait loop. Backoff (1s → 5s cap) keeps the tunnel link quiet; the whole
     * wait window is [PARSE_POLL_TIMEOUT_MS] and [PARSE_POLL_MAX_CONSECUTIVE_FAILURES] network
     * failures in a row end it as [ParseWaitPhase.UNRESOLVED] — never a silently frozen wait.
     * Every tick forces the network read; the five-minute cache must not mask a finished parse.
     */
    private suspend fun pollProjectParse(projectId: String) {
        var delayMs = PARSE_POLL_INITIAL_DELAY_MS
        var consecutiveFailures = 0
        val deadlineNanos = System.nanoTime() + PARSE_POLL_TIMEOUT_MS * 1_000_000L
        while (kotlin.coroutines.coroutineContext.isActive) {
            delay(delayMs)
            delayMs = (delayMs * 2).coerceAtMost(PARSE_POLL_MAX_DELAY_MS)
            when (val result = v25Repository.getProject(projectId, forceRefresh = true)) {
                is V25Result.Success -> {
                    consecutiveFailures = 0
                    projectsById[projectId] = result.value
                    syncProjectMaterial(result.value)
                    _activePdfProject.value = result.value
                    _pdfFile.value = result.value.toPdfFile()
                    when (result.value.status) {
                        V25ProjectStatus.EMPTY -> {
                            _parseWait.value = ParseWaitUiState(projectId = projectId)
                            refreshProjects()
                            return
                        }
                        V25ProjectStatus.PARSING -> if (System.nanoTime() >= deadlineNanos) {
                            _parseWait.value = ParseWaitUiState(
                                projectId = projectId,
                                phase = ParseWaitPhase.UNRESOLVED,
                                reason = "解析仍在进行，已等待超过 3 分钟；可稍后重试或返回项目页查看",
                            )
                            return
                        }
                        V25ProjectStatus.PARSE_FAILED -> {
                            _parseWait.value = parseFailedState(projectId, result.value)
                            refreshProjects()
                            return
                        }
                        V25ProjectStatus.AWAITING_CHAPTER_CONFIRMATION, V25ProjectStatus.READY -> {
                            _parseWait.value = ParseWaitUiState()
                            refreshProjects()
                            return
                        }
                    }
                }
                is V25Result.Failure -> {
                    handleFailure("poll_project_parse", result, surface = false)
                    consecutiveFailures++
                    if (consecutiveFailures >= PARSE_POLL_MAX_CONSECUTIVE_FAILURES) {
                        _parseWait.value = ParseWaitUiState(
                            projectId = projectId,
                            phase = ParseWaitPhase.UNRESOLVED,
                            reason = userMessage(result),
                        )
                        return
                    }
                }
            }
        }
    }

    /**
     * Foreground reconcile for the decoupled parse wait: advance every still-PARSING project's
     * projection once so returning to the app reveals finished parses without re-entering a
     * screen. The WorkManager [com.qiuzhao.flashcards.work.ParseSyncWorker] is the background twin.
     */
    fun reconcileParsingProjects(): Job = viewModelScope.launch {
        refreshProjects(forceRefresh = true).join()
        projectsById.values.filter { it.status == V25ProjectStatus.PARSING }.forEach { project ->
            v25Repository.getProject(project.projectId, forceRefresh = true)
        }
    }

    /**
     * The materials-driven failure state: the project-level PARSE_FAILED verdict is the
     * server's aggregate; the failure cards come from the FAILED PDF materials themselves.
     */
    private fun parseFailedState(projectId: String, project: V25LearningProject): ParseWaitUiState {
        val failed = project.materials
            .filter { it.type == V25MaterialType.PDF && it.status == V25MaterialStatus.FAILED }
            .map { FailedParseMaterial(it.materialId, it.name, it.errorCode) }
        return ParseWaitUiState(
            projectId = projectId,
            phase = ParseWaitPhase.FAILED,
            errorCode = failed.firstNotNullOfOrNull { it.errorCode },
            failedMaterials = failed,
        )
    }

    /**
     * Replaces the FAILED PDF material behind the parse-wait failure (contract: only FAILED PDF
     * materials may replace in place), then re-arms the poller so the screen follows the new
     * parse run.
     */
    fun replaceActiveProjectPdf(uri: Uri, onResult: (Boolean, String?) -> Unit) {
        val projectId = _parseWait.value.projectId ?: _activePdfProject.value?.projectId
        if (projectId == null) {
            onResult(false, "没有待修复的项目")
            return
        }
        val failedMaterial = _parseWait.value.failedMaterials.firstOrNull()
            ?: _activePdfProject.value?.materials
                ?.firstOrNull { it.type == V25MaterialType.PDF && it.status == V25MaterialStatus.FAILED }
                ?.let { FailedParseMaterial(it.materialId, it.name, it.errorCode) }
        if (failedMaterial == null) {
            onResult(false, "没有可替换的失败资料")
            return
        }
        replaceProjectMaterial(projectId, failedMaterial.materialId, uri) { success, message ->
            if (success) startParsePolling(projectId)
            onResult(success, message)
        }
    }

    /** Refreshes active project state while parsing or after leaving a background task. */
    fun refreshActiveProject(projectId: String? = null, onUpdated: (V25LearningProject) -> Unit = {}) = viewModelScope.launch {
        val targetId = projectId ?: _activePdfProject.value?.projectId ?: return@launch
        when (val result = v25Repository.getProject(targetId, forceRefresh = true)) {
            is V25Result.Success -> {
                projectsById[targetId] = result.value
                syncProjectMaterial(result.value)
                _activePdfProject.value = result.value
                _pdfFile.value = result.value.toPdfFile()
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
    ) = viewModelScope.launch {
        val operation = "delete_deck:$deckId"
        val idempotencyKey = beginWrite(operation)
        if (idempotencyKey == null) {
            onFailure()
            return@launch
        }
        var succeeded = false
        try {
            when (val result = v25Repository.deleteDeck(deckId, idempotencyKey)) {
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
        projectCreationCoordinator.reset()
    }

    private fun clearPdfFlow() {
        stopParsePolling()
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
        materialCount = project.materials.size,
        status = project.status.name,
    )

    private fun syncProjectMaterials(projects: List<V25LearningProject>) {
        _projectMaterials.value = projects.associate { project ->
            project.projectId to project.materials.map { it.toProjectMaterial() }
        }
    }

    private fun syncProjectMaterial(project: V25LearningProject) {
        _projectMaterials.value = _projectMaterials.value.toMutableMap().apply {
            put(project.projectId, project.materials.map { it.toProjectMaterial() })
        }
    }

    private fun com.qiuzhao.flashcards.domain.v25.V25Material.toProjectMaterial() = ProjectDraftMaterial(
        id = "project-material-$projectId-$materialId",
        type = if (type == V25MaterialType.PDF) ProjectDraftMaterialType.FILE else ProjectDraftMaterialType.TEXT,
        title = name,
        extension = if (type == V25MaterialType.PDF) {
            name.substringAfterLast('.', "").lowercase().ifBlank { "pdf" }
        } else {
            null
        },
        importedAt = createdAt,
        projectId = projectId,
        materialId = materialId,
        serverStatus = status.name,
        charCount = charCount,
        errorCode = errorCode,
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

    /**
     * Legacy wizard view of the active project: the first PDF material names the "file", and
     * the chapters come from the project's cross-material chapter list (page spans only exist
     * for PDF chapters; TEXT chapters are whole-content and stay out of the wizard).
     */
    private fun V25LearningProject.toPdfFile() = PdfFile(
        id = materials.firstOrNull { it.type == V25MaterialType.PDF }?.materialId.orEmpty(),
        name = materials.firstOrNull { it.type == V25MaterialType.PDF }?.name.orEmpty(),
        status = status.name,
        errorCode = materials.firstOrNull { it.type == V25MaterialType.PDF }?.errorCode,
        chapters = chapters
            .filter { it.startPage != null && it.endPage != null }
            .map { PdfChapter(it.id, it.name, it.startPage!!, it.endPage!!) },
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
