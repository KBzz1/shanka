package com.qiuzhao.flashcards.ui

/**
 * Honest metric projections shared by the deck/data/project pages on the JVM.
 *
 * - A real count below 1000 is displayed as an integer — never as a `0.042k`-style abbreviation
 *   that makes 42 mastered cards read like 42 thousand.
 * - A missing server source stays a dash (`—`); the visual slot is preserved but no value is
 *   invented to fill it.
 */
internal fun honestCount(value: Int?): String = value?.toString() ?: "—"

/** Renders a real ten-percent metric; a missing source stays a dash. */
internal fun honestPercent(value: Int?): String = value?.let { "$it%" } ?: "—"
