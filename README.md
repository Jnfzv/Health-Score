# Customer Health Score

Calcula un **Health Score** por cliente para un SaaS B2B, combinando señales de facturación, soporte y CRM en una puntuación única (0–100) que indica el riesgo de churn o el potencial de expansión.

## ¿Qué hace?

- Extrae datos de **Stripe** (MRR, estado de suscripción, pagos fallidos)
- Extrae datos de **Intercom** (tickets abiertos, tiempo de resolución, sentimiento)
- Extrae datos de **HubSpot** (etapa del ciclo de vida, actividad del contacto)
- Combina las señales con pesos configurables para producir un score por cuenta
- Exporta los resultados a un archivo Excel en `output/`

## Estructura

```
config/          # Pesos del scoring y parámetros configurables
data_sources/    # Conectores a Stripe, Intercom y HubSpot
scoring/         # Lógica de cálculo y normalización del score
output/          # Archivos Excel generados (excluidos de git)
```

## Configuración

1. Copia `.env.example` a `.env` y rellena tus credenciales de API.
2. Instala dependencias:
   ```bash
   pip install -r requirements.txt
   ```
3. Ejecuta el script principal:
   ```bash
   python main.py
   ```

## Variables de entorno

| Variable | Descripción |
|---|---|
| `STRIPE_API_KEY` | Clave secreta de la API de Stripe |
| `INTERCOM_API_TOKEN` | Token de acceso a la API de Intercom |
| `HUBSPOT_API_KEY` | Clave de la API de HubSpot |
