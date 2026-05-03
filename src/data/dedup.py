"""
Article deduplication using rapidfuzz fuzzy string matching.
Groups similar articles about the same story before feeding to the LLM.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from rapidfuzz import fuzz, process

from src.data.models import RawArticle


@dataclass
class ArticleGroup:
    """A cluster of articles that all cover the same story."""
    lead: RawArticle                    # The highest-credibility article
    duplicates: list[RawArticle] = field(default_factory=list)

    @property
    def all_articles(self) -> list[RawArticle]:
        return [self.lead] + self.duplicates

    @property
    def source_count(self) -> int:
        return len(self.all_articles)

    def combined_text(self, max_chars_per_article: int = 800) -> str:
        """Build a combined text blob for LLM ingestion."""
        parts = []
        for i, art in enumerate(self.all_articles):
            # Increase character limit for podcasts to capture more context
            # (Stage 1 model should have enough context for a few thousand chars per podcast)
            limit = max_chars_per_article
            if art.source_type.value == "podcast":
                limit = 5000  # Allow up to 5k chars for transcripts

            text = art.full_text or art.summary or ""
            if len(text) > limit:
                text = text[:limit] + "..."
                
            parts.append(
                f"[Source {i+1}: {art.source_name} ({art.language.upper()}) — {art.published or 'unknown date'}]\n"
                f"Title: {art.title}\n"
                f"{text}"
            )
        return "\n\n---\n\n".join(parts)


def _credibility_rank(article: RawArticle) -> int:
    """Higher = more credible. Used to pick the 'lead' article."""
    rank = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}
    return rank.get(article.credibility.value, 0)


def deduplicate_articles(
    articles: list[RawArticle],
    similarity_threshold: int = 72,
    max_group_size: int = 8,
) -> list[ArticleGroup]:
    """
    Cluster articles into groups where each group represents one story.

    Args:
        articles: List of raw fetched articles
        similarity_threshold: Fuzzy match score 0–100. 72 works well for
                               financial headlines which share domain vocabulary.
        max_group_size: Don't add more than this many duplicates per group
                        (prevents token explosion in LLM context)

    Returns:
        List of ArticleGroup objects, one per unique story
    """
    if not articles:
        return []

    # Sort by credibility descending so high-quality articles become leads
    sorted_articles = sorted(articles, key=_credibility_rank, reverse=True)

    groups: list[ArticleGroup] = []
    assigned: set[int] = set()  # indices of articles already in a group

    for i, article in enumerate(sorted_articles):
        if i in assigned:
            continue

        group = ArticleGroup(lead=article)
        assigned.add(i)

        # Find similar articles not yet assigned
        for j, candidate in enumerate(sorted_articles):
            if j in assigned:
                continue
            if len(group.duplicates) >= max_group_size - 1:
                break

            score = fuzz.token_sort_ratio(
                _normalise(article.title),
                _normalise(candidate.title),
            )

            if score >= similarity_threshold:
                group.duplicates.append(candidate)
                assigned.add(j)

        groups.append(group)

    return groups


def _normalise(text: str) -> str:
    """Lower-case and strip punctuation for better fuzzy matching."""
    import re
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text
