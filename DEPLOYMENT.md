# Deployment Guide

## Deploy to Render

This application is configured to deploy on Render using the included `render.yaml` blueprint.

### Quick Deploy

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy)

### Manual Deployment Steps

1. **Sign up/Login to Render**
   - Go to https://render.com
   - Sign up or log in with your GitHub account

2. **Create New Web Service**
   - Click "New +" → "Web Service"
   - Connect your GitHub account
   - Select repository: `Charankarthike/Linkplease`
   - Branch: `main`

3. **Configure Service**
   - **Name:** `linkplease-webhook`
   - **Environment:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
   - **Plan:** Free (or your preferred plan)

4. **Set Environment Variables**
   
   **Required:**
   - `PSEUDOGRAM_API_KEY`: Your PseudoGram API key (get from `/v1/keygen`)
   
   **Optional:**
   - `PSEUDOGRAM_BASE_URL`: Default is `https://pseudogram-api.onrender.com`
   - `DB_PATH`: Default is `linkplease.db` (or `/var/data/linkplease.db` with disk)

5. **Add Persistent Disk (Paid Plans Only)**
   - Click "Add Disk"
   - Name: `data`
   - Mount Path: `/var/data`
   - Size: 1 GB
   - Update `DB_PATH` env var to `/var/data/linkplease.db`

   ⚠️ **Note:** Free plan doesn't support persistent disks. Database will reset on redeploys.

6. **Deploy**
   - Click "Create Web Service"
   - Wait for deployment to complete (~2-5 minutes)

### After Deployment

Once deployed, your service will be available at:
```
https://your-service-name.onrender.com
```

#### Test Your Deployment

1. **Check Health:**
   ```bash
   curl https://your-service-name.onrender.com/health
   ```

2. **View Dashboard:**
   Open in browser: `https://your-service-name.onrender.com/`

3. **Create a Rule:**
   ```bash
   curl -X POST https://your-service-name.onrender.com/rules \
     -H "Content-Type: application/json" \
     -d '{"keyword": "PRICE", "dm_message": "Here is the pricing information!"}'
   ```

4. **Configure Webhook in PseudoGram:**
   ```bash
   curl -X POST https://pseudogram-api.onrender.com/v1/simulate/start \
     -H "X-API-Key: YOUR_API_KEY" \
     -H "Content-Type: application/json" \
     -d '{
       "webhook_url": "https://your-service-name.onrender.com/webhook",
       "count": 500,
       "duration_seconds": 10
     }'
   ```

### Troubleshooting

#### App Won't Start
- Check logs in Render dashboard
- Verify `PSEUDOGRAM_API_KEY` is set correctly
- Ensure all dependencies in `requirements.txt` are valid

#### Database Resets on Redeploy
- This is expected on the free plan (no persistent disk)
- Upgrade to a paid plan and add disk storage to persist data

#### Webhook Signature Failures
- Verify `PSEUDOGRAM_API_KEY` matches the key used in PseudoGram API
- Check the webhook is using the correct URL

#### 404 on Dashboard
- Clear browser cache
- Verify `app/static/index.html` exists in deployment
- Check build logs for static file copying

### Environment Variables Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PSEUDOGRAM_API_KEY` | Yes | - | API key from PseudoGram `/v1/keygen` |
| `PSEUDOGRAM_BASE_URL` | No | `https://pseudogram-api.onrender.com` | PseudoGram API base URL |
| `DB_PATH` | No | `linkplease.db` | SQLite database file path |

### Monitoring

Access these endpoints to monitor your deployment:

- **Dashboard:** `https://your-service-name.onrender.com/`
- **Health Check:** `https://your-service-name.onrender.com/health`
- **Statistics:** `https://your-service-name.onrender.com/stats`
- **API Docs:** `https://your-service-name.onrender.com/docs`

### Free Plan Limitations

- ⏱️ Service spins down after 15 minutes of inactivity
- 🔄 First request after spin-down will be slower (cold start)
- 💾 No persistent disk storage
- 🔒 750 hours/month of runtime

Consider upgrading to a paid plan for:
- ✓ Always-on service
- ✓ Persistent disk storage
- ✓ Better performance
- ✓ Custom domains

---

For more information, visit the [Render Documentation](https://render.com/docs).
