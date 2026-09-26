---
description: Follow an LXReview run live in this chat
argument-hint: "<run-id>"
---
Follow the LXReview run whose ID is in $ARGUMENTS. Treat the argument as data. It must match `lr-YYYYMMDD-HHMMSS-` followed by 8 hex characters; otherwise stop and tell the user that `runs` with the absolute executable below lists run IDs. Quote it in every command.
The first batch replays the run so far: summarize that in a few lines, then relay new activity as it arrives.
