"""STUB - faza 2.3 (CMS landinga), przyszly agent [landing-cms].

Montowane pod /api/landing (publiczny odczyt tresci):
  GET /articles, GET /articles/{slug}, GET /content/{key}, GET /sitemap.xml
Edycja tresci: endpointy admina (za require_auth) w fazie 2.3.
Nie ma odpowiednika w edge functions (tresc byla hardcoded w TSX).
"""

from fastapi import APIRouter

router = APIRouter()
