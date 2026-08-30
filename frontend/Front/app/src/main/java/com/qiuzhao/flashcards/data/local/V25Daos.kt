package com.qiuzhao.flashcards.data.local

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query
import kotlinx.coroutines.flow.Flow

/**
 * Every query names its `user_id`: account isolation is enforced by the query itself, not by
 * caller discipline. List refreshes delete-then-insert the owned scope inside one transaction
 * (see [V25CacheStore]); a failed network fetch therefore never touches these tables and the
 * last successful cache survives.
 */
@Dao
interface ProjectDao {
    @Query("SELECT * FROM projects WHERE user_id = :userId ORDER BY created_at")
    fun observeProjects(userId: String): Flow<List<ProjectEntity>>

    @Query("SELECT * FROM projects WHERE user_id = :userId ORDER BY created_at")
    suspend fun getProjectList(userId: String): List<ProjectEntity>

    @Query("SELECT * FROM projects WHERE user_id = :userId AND project_id = :projectId")
    suspend fun getProject(userId: String, projectId: String): ProjectEntity?

    @Query("SELECT * FROM project_files WHERE user_id = :userId AND project_id = :projectId")
    suspend fun getFile(userId: String, projectId: String): ProjectFileEntity?

    @Query("SELECT * FROM project_chapters WHERE user_id = :userId AND project_id = :projectId ORDER BY position")
    suspend fun getChapters(userId: String, projectId: String): List<ProjectChapterEntity>

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertProjects(projects: List<ProjectEntity>)

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertFiles(files: List<ProjectFileEntity>)

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertChapters(chapters: List<ProjectChapterEntity>)

    @Query("DELETE FROM projects WHERE user_id = :userId")
    suspend fun deleteProjects(userId: String)

    @Query("DELETE FROM project_files WHERE user_id = :userId")
    suspend fun deleteFiles(userId: String)

    @Query("DELETE FROM project_chapters WHERE user_id = :userId")
    suspend fun deleteChapters(userId: String)

    @Query("DELETE FROM projects WHERE user_id = :userId AND project_id = :projectId")
    suspend fun deleteProject(userId: String, projectId: String)

    @Query("DELETE FROM project_files WHERE user_id = :userId AND project_id = :projectId")
    suspend fun deleteFile(userId: String, projectId: String)

    @Query("DELETE FROM project_chapters WHERE user_id = :userId AND project_id = :projectId")
    suspend fun deleteChaptersOf(userId: String, projectId: String)
}

@Dao
interface DeckDao {
    @Query("SELECT * FROM decks WHERE user_id = :userId ORDER BY name")
    fun observeDecks(userId: String): Flow<List<DeckEntity>>

    @Query("SELECT * FROM decks WHERE user_id = :userId")
    suspend fun getDecks(userId: String): List<DeckEntity>

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertDecks(decks: List<DeckEntity>)

    @Query("DELETE FROM decks WHERE user_id = :userId")
    suspend fun deleteDecks(userId: String)
}

@Dao
interface CardDao {
    @Query("SELECT * FROM cards WHERE user_id = :userId AND deck_id = :deckId ORDER BY position")
    fun observeDeckCards(userId: String, deckId: String): Flow<List<CardEntity>>

    @Query("SELECT * FROM cards WHERE user_id = :userId AND deck_id = :deckId ORDER BY position")
    suspend fun getDeckCards(userId: String, deckId: String): List<CardEntity>

    @Query("SELECT * FROM cards WHERE user_id = :userId AND card_id = :cardId")
    suspend fun getCard(userId: String, cardId: String): CardEntity?

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertCards(cards: List<CardEntity>)

    @Query("DELETE FROM cards WHERE user_id = :userId AND deck_id = :deckId")
    suspend fun deleteDeckCards(userId: String, deckId: String)

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsertReviewStates(states: List<ReviewStateEntity>)

    @Query("SELECT * FROM review_states WHERE user_id = :userId AND card_id = :cardId")
    suspend fun getReviewState(userId: String, cardId: String): ReviewStateEntity?
}

@Dao
interface ReviewQueueDao {
    @Query(
        "SELECT cards.* FROM review_queue JOIN cards ON cards.user_id = review_queue.user_id " +
            "AND cards.card_id = review_queue.card_id WHERE review_queue.user_id = :userId " +
            "AND review_queue.deck_id = :deckId ORDER BY review_queue.position",
    )
    suspend fun getDeckQueue(userId: String, deckId: String): List<CardEntity>

    @Query(
        "SELECT cards.* FROM review_queue JOIN cards ON cards.user_id = review_queue.user_id " +
            "AND cards.card_id = review_queue.card_id WHERE review_queue.user_id = :userId " +
            "AND review_queue.deck_id = :deckId ORDER BY review_queue.position",
    )
    fun observeDeckQueue(userId: String, deckId: String): Flow<List<CardEntity>>

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertQueue(items: List<ReviewQueueItemEntity>)

    @Query("DELETE FROM review_queue WHERE user_id = :userId AND deck_id = :deckId")
    suspend fun deleteQueue(userId: String, deckId: String)

    /** The swipe transaction: hide the card from its deck queue before the card advances. */
    @Query("DELETE FROM review_queue WHERE user_id = :userId AND card_id = :cardId")
    suspend fun removeFromQueue(userId: String, cardId: String)

    @Query("UPDATE today_plan_cards SET hidden = 1 WHERE user_id = :userId AND card_id = :cardId")
    suspend fun hideFromTodayPlan(userId: String, cardId: String)
}

@Dao
interface StudyPlanDao {
    @Query("SELECT * FROM study_plan WHERE user_id = :userId")
    fun observeStudyPlan(userId: String): Flow<StudyPlanEntity?>

    @Query("SELECT * FROM study_plan WHERE user_id = :userId")
    suspend fun getStudyPlan(userId: String): StudyPlanEntity?

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsert(plan: StudyPlanEntity)

    @Query("DELETE FROM study_plan WHERE user_id = :userId")
    suspend fun delete(userId: String)
}

@Dao
interface TodayPlanDao {
    @Query(
        "SELECT * FROM today_plan_cards WHERE user_id = :userId AND study_date = :studyDate " +
            "AND hidden = 0 ORDER BY position",
    )
    suspend fun getVisibleCards(userId: String, studyDate: String): List<TodayPlanCardEntity>

    @Query(
        "SELECT * FROM today_plan_cards WHERE user_id = :userId AND study_date = :studyDate " +
            "AND hidden = 0 ORDER BY position",
    )
    fun observeVisibleCards(userId: String, studyDate: String): Flow<List<TodayPlanCardEntity>>

    @Query("SELECT * FROM today_plan WHERE user_id = :userId AND study_date = :studyDate")
    suspend fun getTodayPlan(userId: String, studyDate: String): TodayPlanEntity?

    @Query("SELECT * FROM today_plan WHERE user_id = :userId AND study_date = :studyDate")
    fun observeTodayPlan(userId: String, studyDate: String): Flow<TodayPlanEntity?>

    @Query("SELECT study_date FROM today_plan WHERE user_id = :userId ORDER BY study_date DESC LIMIT 1")
    suspend fun latestStudyDate(userId: String): String?

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertPlan(plan: TodayPlanEntity)

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun insertCards(cards: List<TodayPlanCardEntity>)

    /** Old study dates are not today's authority; keep the table bounded to the latest date. */
    @Query("DELETE FROM today_plan WHERE user_id = :userId AND study_date != :studyDate")
    suspend fun deleteOtherDates(userId: String, studyDate: String)

    @Query("DELETE FROM today_plan_cards WHERE user_id = :userId AND study_date != :studyDate")
    suspend fun deleteOtherDateCards(userId: String, studyDate: String)

    @Query("SELECT COUNT(*) FROM today_plan WHERE user_id = :userId")
    suspend fun countRows(userId: String): Int
}

@Dao
interface ProgressDao {
    @Query("SELECT * FROM project_progress WHERE user_id = :userId AND project_id = :projectId")
    suspend fun get(userId: String, projectId: String): ProjectProgressEntity?

    @Query("SELECT * FROM project_progress WHERE user_id = :userId")
    fun observe(userId: String): Flow<List<ProjectProgressEntity>>

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsert(progress: ProjectProgressEntity)

    @Query("DELETE FROM project_progress WHERE user_id = :userId AND project_id = :projectId")
    suspend fun delete(userId: String, projectId: String)
}

@Dao
interface DashboardDao {
    @Query("SELECT * FROM dashboard_snapshot WHERE user_id = :userId")
    fun observe(userId: String): Flow<DashboardEntity?>

    @Query("SELECT * FROM dashboard_snapshot WHERE user_id = :userId")
    suspend fun get(userId: String): DashboardEntity?

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsert(dashboard: DashboardEntity)
}

@Dao
interface CacheMetadataDao {
    @Query("SELECT * FROM cache_metadata WHERE user_id = :userId AND resource_key = :resourceKey")
    suspend fun get(userId: String, resourceKey: String): CacheMetadataEntity?

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsert(metadata: CacheMetadataEntity)

    @Query("DELETE FROM cache_metadata WHERE user_id = :userId AND resource_key = :resourceKey")
    suspend fun invalidate(userId: String, resourceKey: String)

    @Query("DELETE FROM cache_metadata WHERE user_id = :userId AND resource_key LIKE :prefix || '%'")
    suspend fun invalidatePrefix(userId: String, prefix: String)
}

@Dao
interface ReviewOutboxDao {
    @Insert(onConflict = OnConflictStrategy.ABORT)
    suspend fun insert(entry: ReviewOutboxEntity)

    /** Strict createdAt order (client_event_id as the deterministic tiebreaker). */
    @Query(
        "SELECT * FROM review_outbox WHERE user_id = :userId AND status = 'PENDING' " +
            "AND next_attempt_at <= :now ORDER BY created_at, client_event_id LIMIT 1",
    )
    suspend fun nextDue(userId: String, now: Long): ReviewOutboxEntity?

    @Query("SELECT * FROM review_outbox WHERE user_id = :userId ORDER BY created_at")
    suspend fun allForUser(userId: String): List<ReviewOutboxEntity>

    @Query("SELECT * FROM review_outbox WHERE user_id = :userId ORDER BY created_at")
    fun observeAll(userId: String): Flow<List<ReviewOutboxEntity>>

    @Query("SELECT COUNT(*) FROM review_outbox WHERE user_id = :userId AND status = 'PENDING'")
    fun observePendingCount(userId: String): Flow<Int>

    @Query("SELECT COUNT(*) FROM review_outbox WHERE user_id = :userId AND status = 'PENDING'")
    suspend fun pendingCount(userId: String): Int

    @Query(
        "UPDATE review_outbox SET status = 'COMPLETED' WHERE user_id = :userId " +
            "AND client_event_id = :clientEventId",
    )
    suspend fun markCompleted(userId: String, clientEventId: String)

    @Query(
        "UPDATE review_outbox SET status = 'FAILED', last_error_code = :errorCode " +
            "WHERE user_id = :userId AND client_event_id = :clientEventId",
    )
    suspend fun markFailed(userId: String, clientEventId: String, errorCode: String)

    @Query(
        "UPDATE review_outbox SET attempt_count = :attemptCount, next_attempt_at = :nextAttemptAt, " +
            "last_error_code = :errorCode WHERE user_id = :userId AND client_event_id = :clientEventId",
    )
    suspend fun scheduleRetry(
        userId: String,
        clientEventId: String,
        attemptCount: Int,
        nextAttemptAt: Long,
        errorCode: String?,
    )
}
