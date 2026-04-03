"""
Container Challenge Plugin

Spawn Docker containers cho challenges với anti-cheat system
"""
import math
import logging
from flask import Flask
from CTFd.plugins import register_plugin_assets_directory
from CTFd.plugins.challenges import CHALLENGE_CLASSES, BaseChallenge
from CTFd.models import db, Solves
from CTFd.utils.modes import get_model
from CTFd.utils.user import get_current_user
from CTFd.utils import get_config

# Import models
from .models import (
    ContainerChallenge,
    ContainerInstance,
    ContainerFlag,
    ContainerFlagAttempt,
    ContainerAuditLog,
    ContainerConfig,
    ContainerFirstBloodAnnounced,
    ContainerAnnouncedSolve,
)

# Import services
from .services import (
    DockerService,
    FlagService,
    ContainerService,
    AntiCheatService,
    AntiCheatService,
    PortManager,
    NotificationService
)

# Import routes
from .routes import user_bp, admin_bp
from .routes.user import set_services as set_user_services
from .routes.admin import set_services as set_admin_services

logger = logging.getLogger(__name__)


class ContainerChallengeType(BaseChallenge):
    """
    Container Challenge Type for CTFd
    
    Spawns Docker containers cho players với:
    - Random hoặc static flags
    - Auto-expiration
    - Anti-cheat detection
    - Resource limits
    """
    id = "container"
    name = "container"
    templates = {
        "create": "/plugins/containers/assets/create.html",
        "update": "/plugins/containers/assets/update.html",
        "view": "/plugins/containers/assets/view.html",
    }
    scripts = {
        "create": "/plugins/containers/assets/create.js",
        "update": "/plugins/containers/assets/update.js",
        "view": "/plugins/containers/assets/view.js",
    }
    route = "/plugins/containers/assets/"
    blueprint = None
    challenge_model = ContainerChallenge
    
    @classmethod
    def create(cls, request):
        """
        Override create to handle field name mapping
        """
        from CTFd.models import db
        
        data = request.form or request.get_json()
        
        # Field mapping from UI to DB attributes
        field_mapping = {
            'initial': 'container_initial',
            'minimum': 'container_minimum',
            'decay': 'container_decay',
            'connection_type': 'container_connection_type',
            'connection_info': 'container_connection_info',
        }
        
        # Fields to exclude (UI-only fields)
        exclude_fields = {'scoring_type'}
        
        # Map field names and exclude UI-only fields
        mapped_data = {}
        for key, value in data.items():
            if key in exclude_fields:
                continue
            mapped_key = field_mapping.get(key, key)
            mapped_data[mapped_key] = value
        
        # Create challenge with mapped data
        challenge = cls.challenge_model(**mapped_data)
        
        # Set initial value
        if 'container_initial' in mapped_data:
            challenge.value = mapped_data['container_initial']
        elif 'initial' in data:
            challenge.value = data['initial']
        
        db.session.add(challenge)
        db.session.commit()
        
        # Create a dummy flag to prevent CTFd from showing "Missing Flags" warning
        # The actual flag validation happens in the solve() method
        from CTFd.models import Flags
        dummy_flag = Flags(
            challenge_id=challenge.id,
            type='static',
            content='[Container flag - auto-generated per instance]',
            data=''
        )
        db.session.add(dummy_flag)
        db.session.commit()
        
        return challenge
    
    @classmethod
    def read(cls, challenge):
        """
        Read challenge data for frontend
        
        Args:
            challenge: ContainerChallenge object
        
        Returns:
            Challenge data dict
        """
        data = {
            "id": challenge.id,
            "name": challenge.name,
            "value": challenge.value,
            "description": challenge.description,
            "category": challenge.category,
            "state": challenge.state,
            "max_attempts": challenge.max_attempts,
            "type": challenge.type,
            "type_data": {
                "id": cls.id,
                "name": cls.name,
                "templates": cls.templates,
                "scripts": cls.scripts,
            },
            # Container specific
            "image": challenge.image,
            "internal_port": challenge.internal_port,
            "connection_type": challenge.container_connection_type,
            "connection_info": challenge.container_connection_info,
            "timeout_minutes": challenge.timeout_minutes,
            "max_renewals": challenge.max_renewals,
            "flag_mode": challenge.flag_mode,
            # Dynamic scoring
            "initial": challenge.container_initial,
            "minimum": challenge.container_minimum,
            "decay": challenge.container_decay,
        }
        return data
    
    @classmethod
    def update(cls, challenge, request):
        """
        Update challenge data
        
        Args:
            challenge: ContainerChallenge object
            request: Flask request object
        
        Returns:
            Updated challenge
        """
        data = request.form or request.get_json()
        
        # Field mapping from UI to DB attributes
        # Note: Some fields have `name=` in column definition for backward compatibility
        field_mapping = {
            'initial': 'container_initial',
            'minimum': 'container_minimum',
            'decay': 'container_decay',
            'connection_type': 'container_connection_type',
            'connection_info': 'container_connection_info',
        }
        
        # Fields to exclude (UI-only fields)
        exclude_fields = {'scoring_type'}
        
        for attr, value in data.items():
            # Skip UI-only fields
            if attr in exclude_fields:
                continue
            
            # Skip if empty
            if value == '':
                continue
            
            # Map field name to actual attribute
            db_attr = field_mapping.get(attr, attr)
            
            # Convert types
            if db_attr in ('container_initial', 'container_minimum', 'container_decay'):
                value = int(value)
            elif db_attr in ('cpu_limit',):
                value = float(value)
            elif db_attr in ('internal_port', 'timeout_minutes', 'max_renewals', 'random_flag_length', 'pids_limit'):
                value = int(value)
            
            setattr(challenge, db_attr, value)
        
        # Set initial value
        if 'initial' in data or 'container_initial' in data:
            challenge.value = challenge.container_initial
        
        # Ensure dummy flag exists to prevent "Missing Flags" warning
        from CTFd.models import Flags, db
        existing_flags = Flags.query.filter_by(challenge_id=challenge.id).count()
        if existing_flags == 0:
            dummy_flag = Flags(
                challenge_id=challenge.id,
                type='static',
                content='[Container flag - auto-generated per instance]',
                data=''
            )
            db.session.add(dummy_flag)
        
        db.session.commit()
        
        return challenge
    
    @classmethod
    def solve(cls, user, team, challenge, request):
        """
        Called when solve is created
        
        Args:
            user: User object
            team: Team object
            challenge: Challenge object
            request: Flask request
        """
        super().solve(user, team, challenge, request)

        # First blood: this solve is the first for this challenge
        is_first_blood = Solves.query.filter_by(challenge_id=challenge.id).count() == 1
        if is_first_blood:
            if notification_service:
                logger.debug("First blood for challenge %s, sending announcement.", challenge.name)
                notification_service.notify_first_blood(user, team, challenge)
                account_id = team.id if (get_config('user_mode') == 'teams' and team) else user.id
                if not ContainerAnnouncedSolve.query.filter_by(challenge_id=challenge.id, account_id=account_id).first():
                    db.session.add(ContainerAnnouncedSolve(challenge_id=challenge.id, account_id=account_id))
                    db.session.commit()
            else:
                logger.warning("First blood detected but notification service not available; announcement skipped.")
        
        # Only recalculate value for dynamic challenges
        # Dynamic challenges have container_decay set
        if challenge.container_decay and challenge.container_decay > 0:
            cls.calculate_value(challenge)
    
    @classmethod
    def attempt(cls, challenge, request):
        """
        Validate flag submission
        
        Args:
            challenge: ContainerChallenge object
            request: Flask request with submission
        
        Returns:
            (is_correct: bool, message: str)
        """
        # Get current user
        user = get_current_user()
        if not user:
            return False, "You must be logged in"
        
        # Get account ID based on mode
        mode = get_config('user_mode')
        is_team_mode = (mode == 'teams')
        
        if is_team_mode:
            if not user.team_id:
                return False, "You must be on a team"
            account_id = user.team_id
        else:
            account_id = user.id
        
        # Get submitted flag
        data = request.form or request.get_json()
        submitted_flag = data.get("submission", "").strip()
        
        if not submitted_flag:
            return False, "No flag provided"
        
        # Use anticheat service to validate
        from . import anticheat_service
        import logging
        logger = logging.getLogger(__name__)
        
        is_correct, message, is_cheating = anticheat_service.validate_flag(
            challenge_id=challenge.id,
            account_id=account_id,
            user_id=user.id,
            submitted_flag=submitted_flag
        )
        
        logger.info(f"Flag validation result: is_correct={is_correct}, message='{message}', is_cheating={is_cheating}")
        
        # If correct, stop container
        if is_correct:
            logger.info(f"Correct flag submitted for challenge {challenge.id} by account {account_id}")
            # Find and stop container instance
            instance = ContainerInstance.query.filter_by(
                challenge_id=challenge.id,
                account_id=account_id,
                status='running'
            ).first()
            
            if instance:
                logger.info(f"Stopping instance {instance.uuid} after successful solve")
                from . import container_service
                try:
                    container_service.stop_instance(instance, user.id, reason='solved')
                    logger.info(f"Successfully stopped instance {instance.uuid}")
                except Exception as e:
                    logger.error(f"Failed to stop instance {instance.uuid}: {e}", exc_info=True)
            else:
                logger.warning(f"No running instance found for challenge {challenge.id}, account {account_id}")
        
        return is_correct, message
    
    @classmethod
    def calculate_value(cls, challenge):
        """
        Calculate dynamic challenge value based on solves
        Supports both linear and logarithmic decay functions
        
        Only applies to dynamic challenges (where container_decay > 0)
        
        Args:
            challenge: ContainerChallenge object
        
        Returns:
            Updated challenge
        """
        # Skip if not a dynamic challenge
        if not challenge.container_decay or challenge.container_decay == 0:
            return challenge
        
        # Skip if missing required dynamic fields
        if not challenge.container_initial or not challenge.container_minimum:
            return challenge
        
        Model = get_model()
        
        solve_count = (
            Solves.query.join(Model, Solves.account_id == Model.id)
            .filter(
                Solves.challenge_id == challenge.id,
                Model.hidden == False,
                Model.banned == False,
            )
            .count()
        )
        
        # Subtract 1 so first solver gets max points
        if solve_count != 0:
            solve_count -= 1
        
        # Get decay function (default to logarithmic for backward compatibility)
        decay_func = getattr(challenge, 'decay_function', 'logarithmic')
        
        if decay_func == 'linear':
            # Linear decay formula
            value = challenge.container_initial - (challenge.container_decay * solve_count)
        else:
            # Logarithmic (parabolic) decay formula
            # Handle division by zero
            decay = challenge.container_decay if challenge.container_decay > 0 else 1
            
            value = (
                ((challenge.container_minimum - challenge.container_initial) / (decay ** 2))
                * (solve_count ** 2)
            ) + challenge.container_initial
        
        value = math.ceil(value)
        
        if value < challenge.container_minimum:
            value = challenge.container_minimum
        
        challenge.value = value
        db.session.commit()
        
        return challenge


# Global service instances
docker_service = None
flag_service = None
container_service = None
anticheat_service = None
port_manager = None
port_manager = None
redis_expiration_service = None
notification_service = None


def load(app: Flask):
    """
    Plugin entry point
    
    Args:
        app: Flask app instance
    """
    global docker_service, flag_service, container_service, anticheat_service, port_manager, redis_expiration_service, notification_service
    
    logger.info("Loading Container Challenge Plugin")
    
    # Create database tables
    app.db.create_all()

    # Backfill container_announced_solves from existing Solves so we don't announce old first bloods/solves
    _backfill_announced_solves()
    
    # Initialize default config
    _initialize_default_config()
    
    # Initialize services
    try:
        # Docker service - Try to initialize but don't fail if socket unavailable
        docker_socket = ContainerConfig.get('docker_socket', 'unix://var/run/docker.sock')
        
        # docker-py handles SSH URLs directly
        docker_service = DockerService(base_url=docker_socket)
        
        # Test connection but don't fail plugin load if unavailable
        if docker_service.is_connected():
            logger.info(f"Docker service initialized and connected: {docker_socket}")
        else:
            logger.warning(f"Docker service initialized but not connected: {docker_socket}")
            logger.warning("Plugin loaded successfully. Configure Docker in Admin → Containers → Settings")
    except Exception as e:
        logger.warning(f"Docker service initialization failed: {e}")
        logger.warning("Plugin loaded successfully. Configure Docker in Admin → Containers → Settings")
        # Create a dummy docker service that will fail gracefully
        try:
            docker_service = DockerService(base_url=docker_socket if 'docker_socket' in locals() else 'unix://var/run/docker.sock')
        except:
            docker_service = None
    
    # Flag service
    flag_service = FlagService()
    logger.info("Flag service initialized")
    
    # Port manager
    port_start = int(ContainerConfig.get('port_range_start', 30000))
    port_end = int(ContainerConfig.get('port_range_end', 31000))
    port_manager = PortManager(port_start, port_end)
    logger.info(f"Port manager initialized: {port_start}-{port_end}")

    # Notification service
    notification_service = NotificationService()
    logger.info("Notification service initialized")
    
    # Container service
    if docker_service:
        container_service = ContainerService(docker_service, flag_service, port_manager, notification_service)
        logger.info("Container service initialized")
    else:
        logger.warning("Container service not initialized (Docker unavailable)")
    
    # Anticheat service
    anticheat_service = AntiCheatService(flag_service, notification_service)
    logger.info("Anticheat service initialized")
    
    # Redis expiration service (for accurate container killing)
    from .services import RedisExpirationService
    redis_expiration_service = RedisExpirationService(
        app=app,
        container_service_getter=lambda: container_service
    )
    redis_expiration_service.start_listener()
    logger.info("Redis expiration service initialized and listener started")
    
    # Register challenge type
    CHALLENGE_CLASSES["container"] = ContainerChallengeType
    logger.info("Registered container challenge type")
    
    # Register plugin assets
    register_plugin_assets_directory(
        app, base_path="/plugins/containers/assets/"
    )
    
    # Register template folder
    from jinja2 import FileSystemLoader, ChoiceLoader
    from os import path
    plugin_dir = path.dirname(__file__)
    template_folder = path.join(plugin_dir, 'templates')
    
    # Add plugin templates to Jinja loader
    if isinstance(app.jinja_loader, ChoiceLoader):
        loaders = list(app.jinja_loader.loaders)
        loaders.insert(0, FileSystemLoader(template_folder))
        app.jinja_loader = ChoiceLoader(loaders)
    else:
        app.jinja_loader = ChoiceLoader([
            FileSystemLoader(template_folder),
            app.jinja_loader
        ])
    logger.info(f"Registered template folder: {template_folder}")
    
    # Inject services into routes
    set_user_services(container_service, flag_service, anticheat_service)
    set_admin_services(docker_service, container_service, anticheat_service)
    
    # Register blueprints
    app.register_blueprint(user_bp)
    app.register_blueprint(admin_bp)
    logger.info("Registered blueprints")
    
    # Setup background jobs
    _setup_background_jobs(app)
    
    logger.info("Container Challenge Plugin loaded successfully")


def _initialize_default_config():
    """Initialize default configuration if not exists"""
    defaults = {
        'docker_socket': 'unix://var/run/docker.sock',
        'connection_host': 'localhost',
        'port_range_start': '30000',
        'port_range_end': '31000',
        'port_allocation_random': 'false',
        'default_timeout': '60',
        'max_renewals': '3',
        'max_memory': '512m',
        'max_cpu': '0.5',
        'container_autoban_enabled': 'true',
        # WaSender WhatsApp
        'wasender_api_key':   '',
        'wasender_group_id':  '',
        'wasender_image_url': '',
        'wasender_audio_url': '',
    }
    
    for key, value in defaults.items():
        if ContainerConfig.get(key) is None:
            ContainerConfig.set(key, value)
            logger.info(f"Set default config: {key}={value}")


def _backfill_announced_solves():
    """
    One-time backfill: insert all current (challenge_id, account_id) from Solves into container_announced_solves
    without sending announcements (reference: handle_past_solves).
    Also migrates existing ContainerFirstBloodAnnounced into ContainerAnnouncedSolve (first solver per challenge).
    """
    try:
        for fb in ContainerFirstBloodAnnounced.query.all():
            first_solve = (
                Solves.query.filter_by(challenge_id=fb.challenge_id)
                .order_by(Solves.date.asc())
                .first()
            )
            if first_solve and first_solve.account_id is not None:
                if not ContainerAnnouncedSolve.query.filter_by(
                    challenge_id=fb.challenge_id,
                    account_id=first_solve.account_id,
                ).first():
                    db.session.add(ContainerAnnouncedSolve(challenge_id=fb.challenge_id, account_id=first_solve.account_id))
        for solve in Solves.query.all():
            if not solve.challenge_id or solve.account_id is None:
                continue
            if ContainerAnnouncedSolve.query.filter_by(
                challenge_id=solve.challenge_id,
                account_id=solve.account_id,
            ).first():
                continue
            db.session.add(ContainerAnnouncedSolve(challenge_id=solve.challenge_id, account_id=solve.account_id))
        db.session.commit()
        logger.info("Backfilled container_announced_solves from existing Solves.")
    except Exception as e:
        logger.warning("Backfill container_announced_solves failed: %s", e)
        db.session.rollback()


def _check_first_blood_announcements():
    """
    Poll for first blood and optional all-solves (reference: SolveHandler.handle_solves).
    Uses container_announced_solves (challenge_id, account_id). First blood = no row for challenge_id.
    """
    from . import notification_service as ns
    if not ns:
        return
    try:
        announce_all = (ContainerConfig.get('container_announce_all_solves', '') or '').strip().lower() == 'true'
        challenge_ids_with_solves = db.session.query(Solves.challenge_id).distinct().all()
        for (cid,) in challenge_ids_with_solves:
            announced_for_chal = ContainerAnnouncedSolve.query.filter_by(challenge_id=cid).all()
            announced_account_ids = {r.account_id for r in announced_for_chal}

            if not announced_for_chal:
                first_solve = (
                    Solves.query.filter_by(challenge_id=cid)
                    .order_by(Solves.date.asc())
                    .first()
                )
                if first_solve and first_solve.user and first_solve.challenge:
                    ns.notify_first_blood(
                        first_solve.user,
                        first_solve.team,
                        first_solve.challenge,
                    )
                    acc_id = first_solve.account_id
                    if not ContainerAnnouncedSolve.query.filter_by(challenge_id=cid, account_id=acc_id).first():
                        db.session.add(ContainerAnnouncedSolve(challenge_id=cid, account_id=acc_id))
                        db.session.commit()
                    announced_account_ids.add(acc_id)
                    logger.info("First blood announced for challenge %s (poller).", first_solve.challenge.name)

            if announce_all:
                solves_for_chal = Solves.query.filter_by(challenge_id=cid).order_by(Solves.date.asc()).all()
                for solve in solves_for_chal:
                    acc_id = solve.account_id
                    if acc_id in announced_account_ids:
                        continue
                    if not solve.user or not solve.challenge:
                        continue
                    ns.announce_solve(solve.user, solve.team, solve.challenge)
                    if not ContainerAnnouncedSolve.query.filter_by(challenge_id=cid, account_id=acc_id).first():
                        db.session.add(ContainerAnnouncedSolve(challenge_id=cid, account_id=acc_id))
                        db.session.commit()
                    announced_account_ids.add(acc_id)
    except Exception as e:
        logger.error("First blood poller error: %s", e, exc_info=True)


def _setup_background_jobs(app):
    """
    Setup background jobs for cleanup
    
    TODO: Sử dụng APScheduler hoặc Celery cho production
    """
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        
        scheduler = BackgroundScheduler()
        
        # First blood poller: like CTFd-First-Blood-Discord (POLL_PERIOD from config)
        poll_period = 30
        try:
            poll_period = max(1, int(ContainerConfig.get('container_first_blood_poll_period', '5') or 5))
        except (TypeError, ValueError):
            pass
        scheduler.add_job(
            func=lambda: _run_with_app_context(app, _check_first_blood_announcements),
            trigger="interval",
            seconds=poll_period,
            id="first_blood_announcements",
        )
        logger.info("Scheduled: first_blood_announcements (every %s seconds)", poll_period)

        # Cleanup expired instances every 1 minute
        if container_service:
            scheduler.add_job(
                func=lambda: _run_with_app_context(app, container_service.cleanup_expired_instances),
                trigger="interval",
                minutes=1,
                id='cleanup_expired'
            )
            logger.info("Scheduled: cleanup_expired_instances (every 1 minute)")
        
        # Cleanup old instances every 1 hour
        if container_service:
            scheduler.add_job(
                func=lambda: _run_with_app_context(app, container_service.cleanup_old_instances),
                trigger="interval",
                hours=1,
                id='cleanup_old'
            )
            logger.info("Scheduled: cleanup_old_instances (every 1 hour)")
        
        scheduler.start()
        
        # Shutdown scheduler on app exit
        import atexit
        atexit.register(lambda: scheduler.shutdown())
        
    except ImportError:
        logger.warning("APScheduler not installed, background jobs disabled")
    except Exception as e:
        logger.error(f"Failed to setup background jobs: {e}")


def _run_with_app_context(app, func):
    """Run function with app context"""
    with app.app_context():
        try:
            func()
        except Exception as e:
            logger.error(f"Error in background job: {e}")
