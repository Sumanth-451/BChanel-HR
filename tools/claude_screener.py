"""
Screen a single candidate against their target role using Claude API.
Returns a structured screening result matching the SCREEN tab schema.
"""
import json
import re
import anthropic
from config.settings import get_settings
from core.logging import get_logger

logger = get_logger("tools.claude_screener")
_settings = get_settings()

_ROLE_REQUIREMENTS = {
    "social media manager": {
        "must_have": ["Meta Ads", "Facebook Ads", "TikTok", "content strategy", "paid advertising"],
        "preferred":  ["Influencer marketing", "Amazon", "Levanta", "community management"],
        "critical_rule": "Missing Meta Ads OR TikTok → score cannot exceed 55",
    },
    "senior amazon ppc expert": {
        "must_have": ["Amazon PPC", "Pay Per Click", "keyword research", "ROAS", "ACOS", "eCommerce"],
        "preferred":  ["DSP", "Sponsored Products", "Sponsored Brands", "A/B testing"],
        "critical_rule": "Missing Amazon PPC or Pay Per Click → score cannot exceed 50",
    },
    "executive assistant": {
        "must_have": ["Diary management", "Administrative support", "scheduling", "communication"],
        "preferred":  ["Google Workspace", "travel management", "stakeholder management"],
        "critical_rule": "Missing diary management AND administrative support → score cannot exceed 45",
    },
}

_SYSTEM_PROMPT = """You are an expert AI hiring screener for BChanel. Evaluate candidates against target roles.

SCORING:
- 70-100: Strong match, core requirements met
- 50-69: Conditional, foundational fit but gaps
- 30-49: Weak match, significant gaps
- 0-29: Do not proceed, fundamental mismatch

WEIGHTS:
- Role readiness (core requirements met): 35%
- Business relevance (industry/domain fit): 25%
- Operational fit (day-to-day task alignment): 20%
- Execution ability + workflow ownership: 10%
- Leadership potential + communication: 10%

Return ONLY valid JSON. No markdown. No code blocks. Start with { end with }."""


def _get_role_context(target_role: str) -> str:
    role_lower = target_role.lower()
    for key, req in _ROLE_REQUIREMENTS.items():
        if key in role_lower:
            return (
                f"MUST HAVE: {', '.join(req['must_have'])}\n"
                f"PREFERRED: {', '.join(req['preferred'])}\n"
                f"CRITICAL RULE: {req['critical_rule']}"
            )
    return "Evaluate against general professional standards for this role."


def screen_candidate(candidate: dict) -> dict:
    """
    Call Claude to screen a single candidate.
    Returns dict matching SCREEN tab columns A-R.
    """
    client = anthropic.Anthropic(api_key=_settings.anthropic_api_key)

    role_context = _get_role_context(candidate.get("target_role", ""))

    user_prompt = f"""Evaluate this candidate.

TARGET ROLE: {candidate.get('target_role', 'Unknown')}
{role_context}

CANDIDATE:
Name: {candidate.get('name', '')}
Email: {candidate.get('email', '')}
Phone: {candidate.get('mobile', '')}
Skills: {candidate.get('skills', '')}
Application Stage: {candidate.get('stage', '')}

Return ONLY valid JSON with these exact fields:
- candidate_name (string)
- current_job_title (string — infer from skills/context if not stated, else empty string)
- target_role (string)
- match_score (integer 0-100)
- recommendation (one of: "Strong Hire", "Hire", "Conditional", "Do Not Advance")
- strengths (array of strings, max 4)
- weaknesses (array of strings, max 4)
- missing_skills (array of strings, max 5)
- interview_focus_areas (array of strings, max 3)
- hiring_risks (array of strings, max 3)
- summary (string, 2-3 sentences)"""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        temperature=0.3,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw = response.content[0].text.strip()
    # Strip markdown fences if Claude wraps anyway
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", raw)
        if match:
            result = json.loads(match.group(0))
        else:
            logger.error("claude_json_parse_failed", raw=raw[:200])
            result = {
                "candidate_name": candidate.get("name", ""),
                "target_role": candidate.get("target_role", ""),
                "match_score": 0,
                "recommendation": "Do Not Advance",
                "strengths": [],
                "weaknesses": ["Screening failed — JSON parse error"],
                "missing_skills": [],
                "interview_focus_areas": [],
                "hiring_risks": ["Rescreen manually"],
                "summary": "Automated screening failed. Please review manually.",
            }

    # Flatten arrays to comma-separated strings for Sheets
    def _join(val):
        if isinstance(val, list):
            return ", ".join(str(v) for v in val)
        return str(val) if val else ""

    return {
        "candidate_id":         candidate.get("record_id", ""),
        "candidate_name":       result.get("candidate_name", candidate.get("name", "")),
        "email":                candidate.get("email", ""),
        "current_job_title":    result.get("current_job_title", "") or candidate.get("current_role", ""),
        "skills":               candidate.get("skills", ""),
        "target_role":          result.get("target_role", candidate.get("target_role", "")),
        "screen_result":        str(result.get("match_score", 0)),
        "recommendation":       result.get("recommendation", ""),
        "strengths":            _join(result.get("strengths", [])),
        "weaknesses":           _join(result.get("weaknesses", [])),
        "missing_skills":       _join(result.get("missing_skills", [])),
        "interview_focus_areas":_join(result.get("interview_focus_areas", [])),
        "hiring_risks":         _join(result.get("hiring_risks", [])),
        "summary":              result.get("summary", ""),
        "zoho_candidate_id":    candidate.get("application_id", ""),
    }
