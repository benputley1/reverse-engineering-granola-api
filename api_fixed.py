"""
FastAPI wrapper for Granola API with persistent token rotation.
Auto-updates Railway env var when WorkOS rotates the refresh token.
"""
import os
import json
import logging
from datetime import datetime, timedelta
from typing import Optional, List
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
import requests

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Granola API",
    description="Reverse-engineered Granola API with persistent token rotation",
    version="1.1.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class TokenState:
    def __init__(self):
        self.access_token: Optional[str] = None
        self.refresh_token: str = os.environ.get("GRANOLA_REFRESH_TOKEN", "")
        self.client_id: str = os.environ.get("GRANOLA_CLIENT_ID", "")
        self.token_expiry: Optional[datetime] = None
        
        # Railway API for persisting rotated tokens
        self.railway_token = os.environ.get("RAILWAY_API_TOKEN", "")
        self.railway_env_id = os.environ.get("RAILWAY_ENVIRONMENT_ID", "")
        self.railway_service_id = os.environ.get("RAILWAY_SERVICE_ID", "")
    
    def is_expired(self) -> bool:
        if not self.access_token or not self.token_expiry:
            return True
        buffer = timedelta(minutes=5)
        return datetime.now() >= (self.token_expiry - buffer)
    
    def _persist_refresh_token(self, new_token: str) -> bool:
        """Update Railway env var with the new rotated refresh token."""
        if not all([self.railway_token, self.railway_env_id, self.railway_service_id]):
            logger.warning("Railway API credentials not configured - token won't persist across restarts")
            return False
        
        try:
            # Railway GraphQL API to update service variable
            url = "https://backboard.railway.app/graphql/v2"
            headers = {
                "Authorization": f"Bearer {self.railway_token}",
                "Content-Type": "application/json"
            }
            
            mutation = """
            mutation($input: VariableUpsertInput!) {
                variableUpsert(input: $input)
            }
            """
            
            variables = {
                "input": {
                    "environmentId": self.railway_env_id,
                    "serviceId": self.railway_service_id,
                    "name": "GRANOLA_REFRESH_TOKEN",
                    "value": new_token
                }
            }
            
            response = requests.post(url, headers=headers, json={
                "query": mutation,
                "variables": variables
            })
            response.raise_for_status()
            
            result = response.json()
            if result.get("errors"):
                logger.error(f"Railway API error: {result['errors']}")
                return False
            
            logger.info("Successfully persisted new refresh token to Railway env")
            return True
        except Exception as e:
            logger.error(f"Failed to persist token to Railway: {e}")
            return False
    
    def refresh(self) -> bool:
        """Exchange refresh token for new access token via WorkOS."""
        if not self.refresh_token or not self.client_id:
            logger.error("Missing GRANOLA_REFRESH_TOKEN or GRANOLA_CLIENT_ID env vars")
            return False
        
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
                
                # CRITICAL: Persist to Railway so it survives restarts
                self._persist_refresh_token(new_refresh)
            
            expires_in = result.get("expires_in", 3600)
            self.token_expiry = datetime.now() + timedelta(seconds=expires_in)
            
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
    return {"status": "ok", "service": "granola-api", "version": "1.1.0"}

@app.get("/health")
async def health():
    token = token_state.get_token()
    return {
        "status": "healthy" if token else "unhealthy",
        "token_valid": token is not None,
        "token_expiry": token_state.token_expiry.isoformat() if token_state.token_expiry else None,
        "railway_persistence": bool(token_state.railway_token)
    }

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
        "limit": 100,  # Fetch more to filter
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
