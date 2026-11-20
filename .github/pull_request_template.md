## What and why

<!-- One or two sentences. Link the issue or decision record if there is one. -->

## Risk checklist

- [ ] Prompts, rate card or policy rules changed? Regenerated `n8n/lead-to-proposal.json` and re-ran `bench/run.py claude` for both twins?
- [ ] Touches a HubSpot write? Which test shows it is still safe to repeat (no duplicate contact or deal)?
- [ ] Touches signing, the claim query or the approval callback? Which test proves the guarantee still holds?
- [ ] Could it put lead content, a token or a secret into logs, errors, the repo or Terraform state?
- [ ] Docs updated: README results, runbook, security questionnaire, ADRs

## How it was tested

<!-- Commands run and what they showed. Say what was real (Claude, HubSpot) and what was a fake. -->
