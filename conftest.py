"""Configurare pytest.

Importul lui multi_tf_strategy construieste clientul Alpaca la nivel de modul.
Punem chei fictive inainte de import ca testele sa ruleze fara .env si fara
sa atinga reteaua — niciun test de aici nu face apeluri catre broker.
"""
import os

os.environ.setdefault("ALPACA_API_KEY", "test")
os.environ.setdefault("ALPACA_SECRET_KEY", "test")
os.environ.setdefault("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
