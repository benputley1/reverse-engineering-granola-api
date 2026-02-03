"""
FastAPI wrapper for Granola API with persistent token rotation.
Uses volume-based file storage to persist tokens across restarts.
"""
import os
import json
import logging
from datetime import datetime, timedelta
from typing import Optional, List
from pathlib import Path
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
import requests

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Granola API",
    description="Reverse-engineered Granola API with persistent token rotation",
    version="1.2.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Persistent token state file (on Railway volume)
TOKEN_STATE_FILE = Path("/data/token_state.json")

class TokenState:
    def __init__(self):
        self.access_token: Optional[str] = None
        self.refresh_token: str = os.environ.get("GRANOLA_REFRESH_TOKEN", "")
        self.client_id: str = os.environ.get("GRANOLA_CLIENT_ID", "")
        self.token_expiry: Optional[datetime] = None
        
        # Try to load persisted state (may have newer refresh token than env var)
        self._load_persisted_state()
    
    def _load_persisted_state(self):
        """Load token state from persistent storage."""
        try:
            if TOKEN_STATE_FILE.exists():
                with open(TOKEN_STATE_FILE, 'r') as f:
                    state = json.load(f)
                
                persisted_refresh = state.get("refresh_token")
                if persisted_refresh:
                    # Use persisted token if it exists (it's more recent than env var)
                    self.refresh_token = persisted_refresh
                    logger.info(f"Loaded persisted refresh token: {persisted_refresh[:10]}...")
                
                # Also restore access token if still valid
                expiry_str = state.get("token_expiry")
                if expiry_str:
                    expiry = datetime.fromisoformat(expiry_str)
                    if datetime.now() < expiry - timedelta(minutes=5):
                        self.access_token = state.get("access_token")
                        self.token_expiry = expiry
                        logger.info("Restored valid access token from persistence")
        except Exception as e:
            logger.warning(f"Could not load persisted state: {e}")
    
    def _persist_state(self):
        """Save token state to persistent storage."""
        try:
            TOKEN_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            state = {
                "refresh_token": self.refresh_token,
                "access_token": self.access_token,
                "token_expiry": self.token_expiry.isoformat() if self.token_expiry else None,
                "updated_at": datetime.now().isoformat()
            }
            with open(TOKEN_STATE_FILE, 'w') as f:
                json.dump(state, f, indent=2)
            logger.info("Persisted token state to volume")
        except Exception as e:
            logger.error(f"Failed to persist token state: {e}")
    
    def is_expired(self) -> bool:
        if not self.access_token or not self.token_expiry:
            return True
        buffer = timedelta(minutes=5)
        return datetime.now() >= (self.token_expiry - buffer)
    
    def refresh(self) -> bool:
        """Exchange refresh token for new access token via WorkOS."""
        if not self.refresh_token:
            logger.error("No refresh token available")
            return False
        if not self.client_id:
            logger.error("No client_id found")
            return False
        
        logger.info(f"Attempting token refresh with: {self.refresh_token[:10]}...")
        
        url = "https://api.workos.com/user_management/authenticate"
        data = {
            "client_id": self.client_id,
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token
        }
        
        try:
            response = requests.post(url, json=data)
            response.raise_for_status()
            result = response.json()
            
            self.access_token = result.get("access_token")
            
            # Handle token rotation - WorkOS rotates refresh tokens
            new_refresh = result.get("refresh_token")
            if new_refresh and new_refresh != self.refresh_token:
                old_prefix = self.refresh_token[:10] if self.refresh_token else "none"
                new_prefix = new_refresh[:10]
                logger.info(f"Refresh token rotated: {old_prefix}... -> {new_prefix}...")
                self.refresh_token = new_refresh
            
            expires_in = result.get("expires_in", 3600)
            self.token_expiry = datetime.now() + timedelta(seconds=expires_in)
            
            # CRITICAL: Persist the new state to survive restarts
            self._persist_state()
            
            logger.info(f"Token refreshed, expires in {expires_in}s")
            return True
        except Exception as e:
            logger.error(f"Token refresh failed: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response: {e.response.text}")
            return False
    
    def get_token(self) -> Optional[str]:
        """Get valid access token, refreshing if needed."""
        if self.is_expired():
            if not self.refresh():
                return None
        return self.access_token

token_state = TokenState()

def get_headers() -> dict:
    """Get headers for Granola API requests."""
    token = token_state.get_token()
    if not token:
        raise HTTPException(status_code=401, detail="Unable to obtain access token")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "User-Agent": "Granola/5.354.0",
        "X-Client-Version": "5.354.0"
    }

# Endpoints
@app.get("/")
async def root():
    return {"status": "ok", "service": "granola-api", "version": "1.2.0"}

@app.get("/health")
async def health():
    token = token_state.get_token()
    persisted = TOKEN_STATE_FILE.exists()
    return {
        "status": "healthy" if token else "unhealthy",
        "token_valid": token is not None,
        "token_expiry": token_state.token_expiry.isoformat() if token_state.token_expiry else None,
        "persistence_enabled": persisted,
        "refresh_token_prefix": token_state.refresh_token[:10] + "..." if token_state.refresh_token else None
    }

@app.post("/reset-token")
async def reset_token():
    """Reset token state and reload from environment variable."""
    global token_state
    # Delete persisted state
    if TOKEN_STATE_FILE.exists():
        TOKEN_STATE_FILE.unlink()
        logger.info("Deleted persisted token state")
    # Reinitialize from env var
    token_state = TokenState()
    # Force a refresh to get new token
    try:
        token_state.refresh()
        return {"status": "ok", "message": "Token reset and refreshed", "refresh_token_prefix": token_state.refresh_token[:10] + "..."}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/documents")
async def list_documents(
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
    include_content: bool = Query(default=True)
):
    """List all documents with pagination."""
    headers = get_headers()
    url = "https://api.granola.ai/v2/get-documents"
    
    data = {
        "limit": limit,
        "offset": offset,
        "include_last_viewed_panel": include_content
    }
    
    try:
        response = requests.post(url, headers=headers, json=data)
        response.raise_for_status()
        result = response.json()
        
        docs = result.get("docs", [])
        simplified = []
        for d in docs:
            simplified.append({
                "id": d.get("id"),
                "title": d.get("title"),
                "created_at": d.get("created_at"),
                "updated_at": d.get("updated_at"),
                "notes_plain": d.get("notes_plain", ""),
                "notes_markdown": d.get("notes_markdown", ""),
                "workspace_id": d.get("workspace_id"),
                "summary": d.get("summary"),
                "overview": d.get("overview"),
                "attendees": _extract_attendees(d),
            })
        
        return {"documents": simplified, "count": len(simplified), "offset": offset}
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/documents/{document_id}")
async def get_document(document_id: str):
    """Get a specific document by ID."""
    headers = get_headers()
    url = "https://api.granola.ai/v1/get-documents-batch"
    
    data = {
        "document_ids": [document_id],
        "include_last_viewed_panel": True
    }
    
    try:
        response = requests.post(url, headers=headers, json=data)
        response.raise_for_status()
        result = response.json()
        
        docs = result.get("documents") or result.get("docs") or []
        if not docs:
            raise HTTPException(status_code=404, detail="Document not found")
        
        d = docs[0]
        return {
            "id": d.get("id"),
            "title": d.get("title"),
            "created_at": d.get("created_at"),
            "updated_at": d.get("updated_at"),
            "notes_plain": d.get("notes_plain", ""),
            "notes_markdown": d.get("notes_markdown", ""),
            "notes": _extract_prosemirror_text(d.get("notes", {})),
            "last_viewed_panel": _extract_prosemirror_text(
                d.get("last_viewed_panel", {}).get("content", {})
            ),
            "workspace_id": d.get("workspace_id"),
            "summary": d.get("summary"),
            "overview": d.get("overview"),
            "attendees": _extract_attendees(d),
            "chapters": d.get("chapters"),
        }
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/documents/{document_id}/transcript")
async def get_transcript(document_id: str):
    """Get transcript for a document."""
    headers = get_headers()
    url = "https://api.granola.ai/v1/get-document-transcript"
    data = {"document_id": document_id}
    
    try:
        response = requests.post(url, headers=headers, json=data)
        if response.status_code == 404:
            raise HTTPException(status_code=404, detail="No transcript available")
        response.raise_for_status()
        return {"transcript": response.json()}
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            raise HTTPException(status_code=404, detail="No transcript available")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/workspaces")
async def list_workspaces():
    """List all workspaces."""
    headers = get_headers()
    url = "https://api.granola.ai/v1/get-workspaces"
    
    try:
        response = requests.post(url, headers=headers, json={})
        response.raise_for_status()
        return {"workspaces": response.json()}
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/folders")
async def list_folders():
    """List all document folders."""
    headers = get_headers()
    
    for url in [
        "https://api.granola.ai/v2/get-document-lists",
        "https://api.granola.ai/v1/get-document-lists"
    ]:
        try:
            response = requests.post(url, headers=headers, json={})
            if response.status_code == 404:
                continue
            response.raise_for_status()
            return {"folders": response.json()}
        except:
            continue
    
    raise HTTPException(status_code=500, detail="Failed to fetch folders")

@app.get("/recent")
async def recent_documents(
    days: int = Query(default=7, le=90),
    limit: int = Query(default=20, le=100)
):
    """Get recent documents from the last N days."""
    headers = get_headers()
    url = "https://api.granola.ai/v2/get-documents"
    
    data = {
        "limit": limit,
        "offset": 0,
        "include_last_viewed_panel": True
    }
    
    try:
        response = requests.post(url, headers=headers, json=data)
        response.raise_for_status()
        result = response.json()
        
        cutoff = datetime.now() - timedelta(days=days)
        docs = []
        for d in result.get("docs", []):
            created = datetime.fromisoformat(d.get("created_at", "").replace("Z", "+00:00"))
            if created.replace(tzinfo=None) >= cutoff:
                docs.append({
                    "id": d.get("id"),
                    "title": d.get("title"),
                    "created_at": d.get("created_at"),
                    "notes_plain": d.get("notes_plain", ""),
                    "notes_markdown": d.get("notes_markdown", ""),
                    "attendees": _extract_attendees(d),
                })
        
        return {"documents": docs, "count": len(docs), "days": days}
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/search")
async def search_documents(
    q: str = Query(..., min_length=1),
    limit: int = Query(default=20, le=100)
):
    """Search documents by keyword."""
    headers = get_headers()
    url = "https://api.granola.ai/v2/get-documents"
    
    data = {
        "limit": 100,
        "offset": 0,
        "include_last_viewed_panel": True
    }
    
    try:
        response = requests.post(url, headers=headers, json=data)
        response.raise_for_status()
        result = response.json()
        
        q_lower = q.lower()
        matches = []
        for d in result.get("docs", []):
            title = (d.get("title") or "").lower()
            notes = (d.get("notes_plain") or "").lower()
            if q_lower in title or q_lower in notes:
                matches.append({
                    "id": d.get("id"),
                    "title": d.get("title"),
                    "created_at": d.get("created_at"),
                    "notes_plain": d.get("notes_plain", "")[:500],
                    "attendees": _extract_attendees(d),
                })
                if len(matches) >= limit:
                    break
        
        return {"documents": matches, "count": len(matches), "query": q}
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=500, detail=str(e))

# Helpers
def _extract_attendees(doc: dict) -> List[str]:
    cal = doc.get("google_calendar_event", {})
    if not cal:
        return []
    attendees = cal.get("attendees", [])
    return [a.get("email", "") for a in attendees if a.get("email")]

def _extract_prosemirror_text(node: dict) -> str:
    if not node:
        return ""
    texts = []
    def recurse(n):
        if isinstance(n, dict):
            if n.get("type") == "text":
                texts.append(n.get("text", ""))
            for child in n.get("content", []):
                recurse(child)
        elif isinstance(n, list):
            for item in n:
                recurse(item)
    recurse(node)
    return " ".join(texts)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
