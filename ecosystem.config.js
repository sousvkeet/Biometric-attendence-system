// pm2 process file (alternative to systemd/Docker):
//   pm2 start ecosystem.config.js && pm2 save && pm2 startup
// Without a reverse proxy the portal listens on all interfaces, port 8090.
module.exports = {
  apps: [
    {
      name: "bioattend-web",
      cwd: __dirname,
      script: ".venv/bin/gunicorn",
      args: "-c deploy/gunicorn.conf.py config.wsgi:application",
      interpreter: "none",
      env: { GUNICORN_BIND: process.env.GUNICORN_BIND || "0.0.0.0:8090" },
    },
    {
      name: "bioattend-worker",
      cwd: __dirname,
      script: ".venv/bin/python",
      args: "manage.py run_worker",
      interpreter: "none",
      instances: 1,           // never more than one worker
      kill_timeout: 30000,
      restart_delay: 10000,
    },
  ],
};
