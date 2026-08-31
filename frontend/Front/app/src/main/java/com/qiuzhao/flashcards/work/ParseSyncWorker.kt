package com.qiuzhao.flashcards.work

import android.content.Context
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import com.qiuzhao.flashcards.FlashcardsApplication
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.CancellationException

/**
 * WorkManager backstop for the decoupled parse wait: advances every still-PARSING project's
 * projection (forced reads) so a finished server-side parse reaches the Room projection even
 * when no screen is polling. Reads are session-scoped inside the repository, so one app-wide
 * unique worker is enough; a signed-out run is a harmless no-op. The foreground ON_RESUME
 * reconcile is the primary path — this worker only covers the app sitting in the background.
 */
class ParseSyncWorker(
    context: Context,
    parameters: WorkerParameters,
) : CoroutineWorker(context, parameters) {

    override suspend fun doWork(): Result = try {
        (applicationContext as FlashcardsApplication).container.v25Repository.refreshParsingProjects()
        Result.success()
    } catch (failure: Throwable) {
        if (failure is CancellationException) throw failure
        // Transient (network/5xx) failures ride WorkManager's exponential backoff.
        Result.retry()
    }

    companion object {
        private const val ONE_TIME_NAME = "parse-sync/on-sign-in"
        private const val PERIODIC_NAME = "parse-sync/periodic"

        /** Sign-in kick: reconcile once soon after connectivity allows. */
        fun enqueueOneTime(context: Context) {
            val request = OneTimeWorkRequestBuilder<ParseSyncWorker>()
                .setConstraints(
                    Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build(),
                )
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 10, TimeUnit.SECONDS)
                .build()
            WorkManager.getInstance(context).enqueueUniqueWork(ONE_TIME_NAME, ExistingWorkPolicy.KEEP, request)
        }

        /** Bounded periodic backstop; WorkManager's floor is 15 minutes. */
        fun enqueuePeriodic(context: Context) {
            val request = PeriodicWorkRequestBuilder<ParseSyncWorker>(15, TimeUnit.MINUTES)
                .setConstraints(
                    Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build(),
                )
                .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 10, TimeUnit.SECONDS)
                .build()
            WorkManager.getInstance(context)
                .enqueueUniquePeriodicWork(PERIODIC_NAME, ExistingPeriodicWorkPolicy.KEEP, request)
        }
    }
}
