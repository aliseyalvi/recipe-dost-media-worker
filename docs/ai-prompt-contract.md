# AI Prompt Contract

## M22E.1R-B: Recipe Tags

The active queue-worker structured extraction contract is `m22e_recipe_tags_v1`.
It uses the existing Gemini structured JSON call; no tag-only Gemini call is made.

The canonical response includes:

```json
{
  "tags": ["chicken", "pakistani", "curry", "spicy", "quick dinner"]
}
```

`tags` is required by the response schema and must contain 5-8 short, searchable
categories. The prompt asks for supported ingredient/protein, cuisine/region,
dish type, method, and taste/use-case categories where applicable. Tags are
lowercase plain text, have no hashtag, are at most 24 characters, and must not
be `food`, `recipe`, `video`, a source username/handle, or a URL.

Before persistence, the worker trims whitespace, strips a leading `#`, lowercases,
deduplicates in first-seen order, removes unsafe/generic values, removes values
over 24 characters, and keeps at most 8 tags. A real AI media job only completes
after the final list has 5-8 tags; an invalid tag result fails safely without a
new Gemini call. Existing cards with `[]` continue to load normally.

The worker writes the final list to the existing `recipe_cards.tags_json` JSONB
field in the repository insert mapper. It never writes source URLs, CDN URLs,
handles, cookies, prompts, or raw Gemini responses as tags.
