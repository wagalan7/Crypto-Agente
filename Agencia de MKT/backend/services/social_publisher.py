import httpx
import os
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class PublishResult:
    platform: str
    success: bool
    post_id: Optional[str] = None
    url: Optional[str] = None
    error: Optional[str] = None


def _sanitize_text(text: str) -> str:
    """Remove non-printable/invisible characters that Meta API rejects."""
    import unicodedata
    cleaned = "".join(
        c for c in text
        if unicodedata.category(c) not in ("Cc", "Cf") or c in ("\n", "\t")
    )
    return cleaned.strip()

async def publish_facebook(text: str, page_id: str, token: str) -> PublishResult:
    text = _sanitize_text(text)
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"https://graph.facebook.com/v19.0/{page_id}/feed",
                json={"message": text, "access_token": token},
            )
            data = resp.json()
            if "id" in data:
                post_id = data["id"]
                return PublishResult(
                    platform="facebook",
                    success=True,
                    post_id=post_id,
                    url=f"https://www.facebook.com/{post_id.replace('_', '/posts/')}",
                )
            return PublishResult(platform="facebook", success=False, error=data.get("error", {}).get("message", str(data)))
    except Exception as e:
        return PublishResult(platform="facebook", success=False, error=str(e))


async def publish_instagram(caption: str, image_url: str, ig_user_id: str, token: str) -> PublishResult:
    caption = _sanitize_text(caption)
    image_url = image_url.strip()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            container = await client.post(
                f"https://graph.facebook.com/v19.0/{ig_user_id}/media",
                json={"caption": caption, "image_url": image_url, "access_token": token},
            )
            c_data = container.json()
            container_id = c_data.get("id")
            if not container_id:
                return PublishResult(platform="instagram", success=False, error=c_data.get("error", {}).get("message", str(c_data)))

            pub = await client.post(
                f"https://graph.facebook.com/v19.0/{ig_user_id}/media_publish",
                json={"creation_id": container_id, "access_token": token},
            )
            p_data = pub.json()
            media_id = p_data.get("id")
            if not media_id:
                return PublishResult(platform="instagram", success=False, error=p_data.get("error", {}).get("message", str(p_data)))

            return PublishResult(
                platform="instagram",
                success=True,
                post_id=media_id,
                url=f"https://www.instagram.com/p/{media_id}/",
            )
    except Exception as e:
        return PublishResult(platform="instagram", success=False, error=str(e))


async def publish_twitter(text: str, bearer_token: str, api_key: str, api_secret: str, access_token: str, access_secret: str) -> PublishResult:
    try:
        import hmac, hashlib, uuid, base64, urllib.parse

        url = "https://api.twitter.com/2/tweets"
        oauth_params = {
            "oauth_consumer_key": api_key,
            "oauth_nonce": uuid.uuid4().hex,
            "oauth_signature_method": "HMAC-SHA1",
            "oauth_timestamp": str(int(time.time())),
            "oauth_token": access_token,
            "oauth_version": "1.0",
        }

        base_string = "&".join([
            "POST",
            urllib.parse.quote(url, safe=""),
            urllib.parse.quote("&".join(f"{k}={urllib.parse.quote(v,safe='')}" for k, v in sorted(oauth_params.items())), safe=""),
        ])
        signing_key = f"{urllib.parse.quote(api_secret, safe='')}&{urllib.parse.quote(access_secret, safe='')}"
        signature = base64.b64encode(hmac.new(signing_key.encode(), base_string.encode(), hashlib.sha1).digest()).decode()
        oauth_params["oauth_signature"] = signature

        auth_header = "OAuth " + ", ".join(f'{k}="{urllib.parse.quote(v, safe="")}"' for k, v in sorted(oauth_params.items()))

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(url, json={"text": text}, headers={"Authorization": auth_header, "Content-Type": "application/json"})
            data = resp.json()
            tweet_id = data.get("data", {}).get("id")
            if tweet_id:
                return PublishResult(platform="twitter", success=True, post_id=tweet_id, url=f"https://twitter.com/i/web/status/{tweet_id}")
            return PublishResult(platform="twitter", success=False, error=str(data))
    except Exception as e:
        return PublishResult(platform="twitter", success=False, error=str(e))


async def publish_webhook(payload: dict, webhook_url: str) -> PublishResult:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(webhook_url, json=payload)
            return PublishResult(platform="webhook", success=resp.status_code < 300, url=webhook_url)
    except Exception as e:
        return PublishResult(platform="webhook", success=False, error=str(e))


async def publish_facebook_ads(
    text: str,
    page_id: str,
    token: str,
    ad_account_id: str,
    final_url: str,
    budget_amount: str = "20",
    objective: str = "LINK_CLICKS",
) -> PublishResult:
    """
    Cria uma campanha paga no Facebook Ads (Marketing API v19.0).
    Criada em status PAUSED para revisão antes de ativar.
    """
    try:
        # Parse budget (R$ → cents in BRL)
        try:
            amount = float(
                budget_amount.replace("R$", "").replace("r$", "")
                             .replace(",", ".").split("/")[0].strip()
            )
        except Exception:
            amount = 20.0
        daily_budget_cents = int(amount * 100)   # Facebook uses local currency * 100

        ts = int(time.time())
        act = f"act_{ad_account_id.lstrip('act_')}"
        base = f"https://graph.facebook.com/v19.0"

        # Map objective to billing/optimization
        OBJ_MAP = {
            "LINK_CLICKS":   ("LINK_CLICKS", "LINK_CLICKS", "IMPRESSIONS"),
            "CONVERSIONS":   ("OFFSITE_CONVERSIONS", "OFFSITE_CONVERSIONS", "IMPRESSIONS"),
            "REACH":         ("REACH", "REACH", "IMPRESSIONS"),
            "BRAND_AWARENESS": ("BRAND_AWARENESS", "REACH", "IMPRESSIONS"),
            "ENGAGEMENT":    ("POST_ENGAGEMENT", "POST_ENGAGEMENT", "IMPRESSIONS"),
        }
        obj_key = objective if objective in OBJ_MAP else "LINK_CLICKS"
        fb_objective, opt_goal, billing_event = OBJ_MAP[obj_key]

        if not final_url or not final_url.startswith("http"):
            final_url = "https://example.com"

        async with httpx.AsyncClient(timeout=30) as http:

            # 1. Create Campaign
            r = await http.post(f"{base}/{act}/campaigns", data={
                "name":      f"Campanha MagaOne {ts}",
                "objective": fb_objective,
                "status":    "PAUSED",
                "access_token": token,
            })
            rd = r.json()
            campaign_id = rd.get("id", "")
            if not campaign_id:
                return PublishResult(platform="facebook_ads", success=False,
                    error=f"Erro ao criar campanha: {rd.get('error', {}).get('message', str(rd))}")

            # 2. Create Ad Set
            r = await http.post(f"{base}/{act}/adsets", data={
                "name":              f"AdSet MagaOne {ts}",
                "campaign_id":       campaign_id,
                "daily_budget":      str(daily_budget_cents),
                "billing_event":     billing_event,
                "optimization_goal": opt_goal,
                "targeting":         '{"geo_locations":{"countries":["BR"]},"age_min":18,"age_max":65}',
                "status":            "PAUSED",
                "access_token":      token,
            })
            rd = r.json()
            adset_id = rd.get("id", "")
            if not adset_id:
                return PublishResult(platform="facebook_ads", success=False,
                    error=f"Erro ao criar conjunto: {rd.get('error', {}).get('message', str(rd))}")

            # 3. Create Ad Creative (link ad)
            lines  = [l.strip() for l in text.split("\n") if l.strip()]
            title  = lines[0][:40] if lines else "Conheça agora"
            body   = " ".join(lines[1:3])[:90] if len(lines) > 1 else text[:90]
            story  = {
                "page_id": page_id,
                "link_data": {
                    "message":     text[:600],
                    "link":        final_url,
                    "name":        title,
                    "description": body,
                    "call_to_action": {"type": "LEARN_MORE", "value": {"link": final_url}},
                },
            }
            import json as _json
            r = await http.post(f"{base}/{act}/adcreatives", data={
                "name":                f"Creative MagaOne {ts}",
                "object_story_spec":   _json.dumps(story),
                "access_token":        token,
            })
            rd = r.json()
            creative_id = rd.get("id", "")
            if not creative_id:
                return PublishResult(platform="facebook_ads", success=False,
                    error=f"Erro ao criar criativo: {rd.get('error', {}).get('message', str(rd))}")

            # 4. Create Ad
            r = await http.post(f"{base}/{act}/ads", data={
                "name":      f"Anúncio MagaOne {ts}",
                "adset_id":  adset_id,
                "creative":  _json.dumps({"creative_id": creative_id}),
                "status":    "PAUSED",
                "access_token": token,
            })
            rd = r.json()
            ad_id = rd.get("id", "")
            if not ad_id:
                return PublishResult(platform="facebook_ads", success=False,
                    error=f"Erro ao criar anúncio: {rd.get('error', {}).get('message', str(rd))}")

        return PublishResult(
            platform="facebook_ads",
            success=True,
            post_id=campaign_id,
            url=f"https://www.facebook.com/adsmanager/manage/campaigns?act={act}&selected_campaign_ids={campaign_id}",
        )

    except Exception as e:
        return PublishResult(platform="facebook_ads", success=False, error=str(e))


async def toggle_facebook_ads_campaign(
    campaign_id: str,
    new_status: str,    # "ACTIVE" or "PAUSED"
    token: str,
) -> dict:
    """Pause or activate a Facebook Ads campaign."""
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"https://graph.facebook.com/v19.0/{campaign_id}",
                data={"status": new_status, "access_token": token},
            )
            data = r.json()
        if data.get("success"):
            return {"success": True, "status": new_status}
        return {"success": False, "error": data.get("error", {}).get("message", str(data))}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def toggle_google_ads_campaign(
    campaign_id: str,
    new_status: str,           # "ENABLED" or "PAUSED"
    developer_token: str,
    customer_id: str,
    refresh_token: str,
    client_id: str,
    client_secret: str,
    mcc_id: str = "",
) -> dict:
    """Pause or activate a Google Ads campaign by ID."""
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            tr = await c.post("https://oauth2.googleapis.com/token", data={
                "client_id": client_id, "client_secret": client_secret,
                "refresh_token": refresh_token, "grant_type": "refresh_token",
            })
            tokens = tr.json()
        access_token = tokens.get("access_token", "")
        if not access_token:
            return {"success": False, "error": tokens.get("error_description", "Token inválido")}

        cid = customer_id.replace("-", "").replace(" ", "")
        headers = {
            "Authorization":   f"Bearer {access_token}",
            "developer-token": developer_token,
            "Content-Type":    "application/json",
        }
        clean_mcc = mcc_id.replace("-", "").replace(" ", "")
        if clean_mcc:
            headers["login-customer-id"] = clean_mcc

        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"https://googleads.googleapis.com/v19/customers/{cid}/campaigns:mutate",
                headers=headers,
                json={"operations": [{"update": {
                    "resourceName": f"customers/{cid}/campaigns/{campaign_id}",
                    "status": new_status,
                }, "updateMask": "status"}]},
            )
            data = r.json()

        if "error" in data:
            return {"success": False, "error": data["error"].get("message", str(data["error"]))}
        return {"success": True, "status": new_status}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def publish_google_ads(
    text: str,
    developer_token: str,
    customer_id: str,
    refresh_token: str,
    final_url: str,
    budget_amount: str = "20",
    mcc_id: str = "",
    keywords: list = [],
    location_id: str = "2076",
) -> PublishResult:
    """
    Cria uma campanha de pesquisa no Google Ads com Responsive Search Ad.
    A campanha é criada em status PAUSED para revisão antes de ativar.
    """
    try:
        client_id     = os.getenv("GOOGLE_CLIENT_ID", "")
        client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "")

        # ── 1. Trocar refresh token por access token ───────────────
        async with httpx.AsyncClient(timeout=20) as http:
            tr = await http.post("https://oauth2.googleapis.com/token", data={
                "client_id":     client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type":    "refresh_token",
            })
            tokens = tr.json()

        access_token = tokens.get("access_token", "")
        if not access_token:
            return PublishResult(platform="google", success=False,
                                 error=f"Erro ao obter access token: {tokens.get('error_description', tokens.get('error', 'desconhecido'))}")

        # ── Setup ──────────────────────────────────────────────────
        cid = customer_id.replace("-", "").replace(" ", "")
        base = f"https://googleads.googleapis.com/v19/customers/{cid}"
        headers = {
            "Authorization":   f"Bearer {access_token}",
            "developer-token": developer_token,
            "Content-Type":    "application/json",
        }
        # When using an MCC developer token to access a sub-account,
        # login-customer-id must be set to the MCC account ID.
        clean_mcc = mcc_id.replace("-", "").replace(" ", "")
        if clean_mcc:
            headers["login-customer-id"] = clean_mcc

        # Parse budget (R$ → micros)
        try:
            amount = float(
                budget_amount.replace("R$", "").replace("r$", "")
                             .replace(",", ".").split("/")[0].strip()
            )
        except Exception:
            amount = 20.0
        budget_micros = int(amount * 1_000_000)

        ts = int(time.time())

        def _json(resp) -> dict:
            """Parse JSON safely — returns human-readable error dict on failures."""
            if resp.status_code == 404:
                return {"error": {"message": (
                    "Conta Google Ads não encontrada (404). Verifique: "
                    "(1) Customer ID correto (sem hífens), "
                    "(2) conta totalmente ativada com método de pagamento em ads.google.com, "
                    "(3) se usa MCC, preencha o campo 'ID da Conta MCC' nas credenciais."
                )}}
            if resp.status_code == 403:
                return {"error": {"message": (
                    "Acesso negado (403). Verifique se o Developer Token tem permissão "
                    "para acessar esta conta e se a conta MCC está vinculada."
                )}}
            if resp.status_code == 401:
                return {"error": {"message": (
                    "Token inválido (401). Reconecte a conta Google Ads via OAuth "
                    "nas configurações de credenciais."
                )}}
            try:
                return resp.json()
            except Exception:
                return {"error": {"message": f"HTTP {resp.status_code}: {resp.text[:200] or '(resposta vazia)'}"}}

        async with httpx.AsyncClient(timeout=30) as http:

            # ── 2. Criar budget ────────────────────────────────────
            r = await http.post(f"{base}/campaignBudgets:mutate", headers=headers, json={
                "operations": [{"create": {
                    "name":           f"Budget MagaOne {ts}",
                    "amountMicros":   str(budget_micros),
                    "deliveryMethod": "STANDARD",
                }}]
            })
            rd = _json(r)
            budget_resource = rd.get("results", [{}])[0].get("resourceName", "")
            if not budget_resource:
                return PublishResult(platform="google", success=False,
                                     error=f"Erro ao criar budget: {rd.get('error', {}).get('message', str(rd))}")

            # ── 3. Criar campanha ──────────────────────────────────
            r = await http.post(f"{base}/campaigns:mutate", headers=headers, json={
                "operations": [{"create": {
                    "name":                    f"Campanha MagaOne {ts}",
                    "status":                  "PAUSED",
                    "advertisingChannelType":  "SEARCH",
                    "campaignBudget":          budget_resource,
                    "manualCpc":               {"enhancedCpcEnabled": False},
                    "networkSettings": {
                        "targetGoogleSearch":  True,
                        "targetSearchNetwork": True,
                    },
                    "startDate": time.strftime("%Y%m%d"),
                }}]
            })
            rd = _json(r)
            campaign_resource = rd.get("results", [{}])[0].get("resourceName", "")
            if not campaign_resource:
                return PublishResult(platform="google", success=False,
                                     error=f"Erro ao criar campanha: {rd.get('error', {}).get('message', str(rd))}")
            campaign_id = campaign_resource.split("/")[-1]

            # ── 4. Criar Ad Group ──────────────────────────────────
            cpc_micros = max(500_000, budget_micros // 10)  # mín R$0,50
            r = await http.post(f"{base}/adGroups:mutate", headers=headers, json={
                "operations": [{"create": {
                    "name":          f"Grupo MagaOne {ts}",
                    "campaign":      campaign_resource,
                    "status":        "ENABLED",
                    "cpcBidMicros":  str(cpc_micros),
                }}]
            })
            rd = _json(r)
            adgroup_resource = rd.get("results", [{}])[0].get("resourceName", "")
            if not adgroup_resource:
                return PublishResult(platform="google", success=False,
                                     error=f"Erro ao criar grupo: {rd.get('error', {}).get('message', str(rd))}")

            # ── 5. Montar headlines e descriptions ─────────────────
            lines = [l.strip() for l in text.replace("\n\n", "\n").split("\n") if l.strip()]
            headlines:    list[dict] = []
            descriptions: list[dict] = []

            for line in lines:
                clean = line.lstrip("•*-#123456789. ").strip()
                if not clean:
                    continue
                if len(clean) <= 30 and len(headlines) < 15:
                    headlines.append({"text": clean})
                elif len(clean) <= 90 and len(descriptions) < 4:
                    descriptions.append({"text": clean[:90]})

            # Garantir mínimos obrigatórios
            fallback_h = ["Conheça Agora", "Oferta Especial", "Saiba Mais"]
            fallback_d = [text[:90] if text else "Descubra nosso produto.", "Acesse e saiba mais."]
            for fh in fallback_h:
                if len(headlines) >= 3:
                    break
                headlines.append({"text": fh})
            for fd in fallback_d:
                if len(descriptions) >= 2:
                    break
                descriptions.append({"text": fd})

            # Garantir URL final
            if not final_url or not final_url.startswith("http"):
                final_url = "https://example.com"

            # ── 6. Criar Responsive Search Ad ─────────────────────
            r = await http.post(f"{base}/adGroupAds:mutate", headers=headers, json={
                "operations": [{"create": {
                    "adGroup": adgroup_resource,
                    "status":  "ENABLED",
                    "ad": {
                        "finalUrls": [final_url],
                        "responsiveSearchAd": {
                            "headlines":    headlines[:15],
                            "descriptions": descriptions[:4],
                        },
                    },
                }}]
            })
            rd = _json(r)
            ad_resource = rd.get("results", [{}])[0].get("resourceName", "")
            if not ad_resource:
                return PublishResult(platform="google", success=False,
                                     error=f"Erro ao criar anúncio: {rd.get('error', {}).get('message', str(rd))}")

            # ── 7. Geo targeting ──────────────────────────────────
            loc_id = location_id.strip() if location_id else "2076"
            await http.post(f"{base}/campaignCriteria:mutate", headers=headers, json={
                "operations": [{"create": {
                    "campaign": campaign_resource,
                    "location": {"geoTargetConstant": f"geoTargetConstants/{loc_id}"},
                }}]
            })

            # ── 8. Keywords ────────────────────────────────────────
            kws = [k.strip() for k in (keywords or []) if k.strip()]
            if kws:
                kw_ops = []
                for kw in kws[:20]:   # max 20 keywords
                    match = "EXACT" if kw.startswith("[") and kw.endswith("]") else \
                            "PHRASE" if kw.startswith('"') and kw.endswith('"') else "BROAD"
                    clean_kw = kw.strip('[]"')
                    kw_ops.append({"create": {
                        "adGroup": adgroup_resource,
                        "status":  "ENABLED",
                        "keyword": {"text": clean_kw[:80], "matchType": match},
                    }})
                await http.post(f"{base}/adGroupCriteria:mutate", headers=headers,
                                json={"operations": kw_ops})

        return PublishResult(
            platform="google",
            success=True,
            post_id=campaign_id,
            url=f"https://ads.google.com/aw/campaigns?campaignId={campaign_id}",
        )

    except Exception as e:
        return PublishResult(platform="google", success=False, error=str(e))
