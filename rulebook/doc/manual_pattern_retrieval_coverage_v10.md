# Manual Pattern Retrieval Coverage v10

This is the B4 codebase-only 13-case verification run after allowing admission
to tolerate malformed grounded anchors when at least two well-formed anchors
remain.

## Summary

| metric | value |
|---|---:|
| databases | 1 |
| candidates | 9 |
| complete candidates | 3 |
| mixed candidates | 2 |
| manual patterns | 4 |
| patterns fully co-candidate | 2 |
| no full co-candidate | 2 |
| no pair co-candidate | 1 |

## Phase 1 Gate

| check | expected | actual | status |
|---|---|---:|---|
| complete candidates | >= 5 | 3 | FAIL |
| mixed candidates | <= 1 | 2 | FAIL |
| no pair co-candidate | <= 1 | 1 | PASS |
| toxicology bond_pair complete | complete | missing | FAIL |
| codebase posthistory complete | complete | no (6/7) | FAIL |
| admission_judge calls | >= 5 | 9 | PASS |

## codebase_community

| metric | value |
|---|---:|
| candidates | 9 |
| complete candidates | 3 |
| mixed candidates | 2 |
| patterns fully co-candidate | 2 / 4 |
| no full co-candidate | 2 |
| no pair co-candidate | 1 |

| pattern | full | max co-cases | pair coverage | fragments |
|---|---|---:|---:|---:|
| `code_editor_to_owner_user` | no | 0/2 | 0/1 | 0 |
| `code_user_post_relation_via_posthistory` | no | 6/7 | 17/21 | 6 |
| `code_comment_created_on_comments_creationdate` | yes | 2/2 | 1/1 | 3 |
| `code_comment_score_filter_on_posts_score` | yes | 2/2 | 1/1 | 1 |

### Complete Candidates

- cases=['616', '617'] patterns=['code_comment_created_on_comments_creationdate']
- cases=['616', '617', '709'] patterns=['code_comment_created_on_comments_creationdate']
- cases=['616', '617', '709', '710'] patterns=['code_comment_created_on_comments_creationdate', 'code_comment_score_filter_on_posts_score']

### Mixed Candidates

- cases=['616', '617', '709'] touched=['code_comment_created_on_comments_creationdate', 'code_comment_score_filter_on_posts_score']
- cases=['616', '617', '709', '710'] touched=['code_comment_created_on_comments_creationdate', 'code_comment_score_filter_on_posts_score']

## Actual Pattern Snapshot

| actual pattern | cases | manual pattern coverage | mixed? |
|---|---|---|---|
| `grp-pat-codebase_community-602-652-338284ca` | `[602,631,652]` | `code_user_post_relation_via_posthistory` | no |
| `grp-pat-codebase_community-616-710-5e22a456` | `[616,617,709,710]` | `code_comment_created_on_comments_creationdate`, `code_comment_score_filter_on_posts_score` | yes |
| `grp-pat-codebase_community-631-632-026c8df4` | `[631,632]` | `code_user_post_relation_via_posthistory` | no |
| `grp-pat-codebase_community-632-635-558b0f65` | `[632,635]` | `code_user_post_relation_via_posthistory` | no |

## B4 Gate Notes

- Codebase actual pattern count improved from v9 `1` to v10 `4`.
- Posthistory max co-candidate improved to `6/7`, but the largest actual pure posthistory pattern is only `3/7`.
- The requested 4-case posthistory pattern `[631,632,635,639]` was not recovered as an actual pattern.
- A new mixed actual pattern appeared: `[616,617,709,710]`, crossing comment-created and comment-score manual patterns.
- This trips the stop condition: B4 loosened the anchor gate but introduced pattern pollution.
