package com.qiuzhao.flashcards.data.local

import android.content.Context
import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase
import androidx.room.migration.Migration

/**
 * `shanka-v25.db` — the local-fact projection of server V2.5 state. Schema is exported
 * (`app/schemas`, kapt `room.schemaLocation`) and version bumps must ship an explicit
 * [MIGRATIONS] entry; [fallbackToDestructiveMigration] is deliberately never called, so a
 * missing migration crashes loudly instead of silently wiping the user's cached facts.
 */
@Database(
    entities = [
        ProjectEntity::class,
        ProjectFileEntity::class,
        ProjectChapterEntity::class,
        DeckEntity::class,
        CardEntity::class,
        ReviewStateEntity::class,
        ReviewQueueItemEntity::class,
        StudyPlanEntity::class,
        TodayPlanEntity::class,
        TodayPlanCardEntity::class,
        ProjectProgressEntity::class,
        DashboardEntity::class,
        CacheMetadataEntity::class,
        ReviewOutboxEntity::class,
    ],
    version = ShankaV25Database.VERSION,
    exportSchema = true,
)
abstract class ShankaV25Database : RoomDatabase() {
    abstract fun projectDao(): ProjectDao
    abstract fun deckDao(): DeckDao
    abstract fun cardDao(): CardDao
    abstract fun reviewQueueDao(): ReviewQueueDao
    abstract fun studyPlanDao(): StudyPlanDao
    abstract fun todayPlanDao(): TodayPlanDao
    abstract fun progressDao(): ProgressDao
    abstract fun dashboardDao(): DashboardDao
    abstract fun cacheMetadataDao(): CacheMetadataDao
    abstract fun reviewOutboxDao(): ReviewOutboxDao

    companion object {
        const val NAME = "shanka-v25.db"
        const val VERSION = 1

        /** Projection schema version written into cache metadata rows. */
        const val CACHE_SCHEMA_VERSION = 1

        /**
         * Explicit migrations only. Bump [VERSION] → add the Migration here → the exported
         * schema lands in `app/schemas` in the same change. Destructive fallback is banned.
         */
        val MIGRATIONS: Array<Migration> = arrayOf(
            // v1 → v2 (none yet): Migration(1, 2) { db: SupportSQLiteDatabase -> … }
        )

        fun build(context: Context): ShankaV25Database =
            Room.databaseBuilder(context.applicationContext, ShankaV25Database::class.java, NAME)
                .addMigrations(*MIGRATIONS)
                .build()

        /**
         * Robolectric/JVM file-backed builder: a real SQLite file at [path], so a test can
         * close the process-level objects, rebuild them and still observe persisted rows.
         */
        fun buildOnFile(context: Context, path: String): ShankaV25Database =
            Room.databaseBuilder(context, ShankaV25Database::class.java, path)
                .addMigrations(*MIGRATIONS)
                .build()
    }
}
