from flask import render_template, redirect, url_for, jsonify, request
from flask_babelex import lazy_gettext, gettext
from lidarts.game import bp
from lidarts.game.forms import CreateX01GameForm, ScoreForm, GameChatmessageForm
from lidarts.models import Game, User, Notification, ChatmessageIngame, Bot, Stats
from lidarts import db
from lidarts.socket.utils import broadcast_game_aborted, broadcast_new_game, send_notification, calculate_statistics
from lidarts.game.utils import get_name_by_id, collect_statistics
from lidarts.socket.X01_game_handler import start_game
from flask_login import current_user, login_required
from datetime import datetime
import json


@bp.route('/create', methods=['GET', 'POST'])
@bp.route('/create/<mode>', methods=['GET', 'POST'])
@bp.route('/create/<mode>/<opponent_name>', methods=['GET', 'POST'])
@login_required
def create(mode='x01', opponent_name=None):
    if mode == 'x01':
        form = CreateX01GameForm(opponent_name=opponent_name)
    else:
        pass  # no other game modes yet
    if form.validate_on_submit():
        player1 = current_user.id if current_user.is_authenticated else None
        if player1 and form.opponent.data == 'local':
            player2 = current_user.id
            status = 'started'
        elif player1 and form.opponent.data == 'online':
            if form.opponent_name.data:
                player2 = User.query.with_entities(User.id).filter_by(username=form.opponent_name.data).first_or_404()
                message = gettext('New challenge')
                notification = Notification(user=player2, message=message, author=current_user.username,
                                            type='challenge')
                db.session.add(notification)
                send_notification(form.opponent_name.data, message, current_user.username, 'challenge')
            else:
                player2 = None
            status = 'challenged'
        else:
            # computer as opponent
            player2 = None
            status = 'started'
        match_json = json.dumps({1: {1: {1: {'scores': [], 'double_missed': []},
                                         2: {'scores': [], 'double_missed': []}}}})
        game = Game(player1=player1, player2=player2, type=form.type.data,
                    bo_sets=form.bo_sets.data, bo_legs=form.bo_legs.data,
                    p1_sets=0, p2_sets=0, p1_legs=0, p2_legs=0,
                    p1_score=int(form.type.data), p2_score=int(form.type.data),
                    in_mode=form.in_mode.data, out_mode=form.out_mode.data,
                    begin=datetime.utcnow(), match_json=match_json,
                    status=status, opponent_type=form.opponent.data)
        if game.opponent_type.startswith('computer'):
            if form.bot_choice.data == 'level':
                game.bot_id = form.level.data
            else:
                bot_name = '[Bot] {username}'.format(username=current_user.username)

                bot = Bot(name=bot_name, sigma_x=sigma_x, sigma_y=sigma_y)
                db.session.add(bot)
                db.session.commit()
                game.bot_id = bot.id
        game.p1_next_turn = form.starter.data == 'me'
        if form.starter.data == 'closest_to_bull':
            game.p1_next_turn = True
            closest_to_bull_json = json.dumps({1: [], 2: []})
            game.closest_to_bull_json = closest_to_bull_json
            game.closest_to_bull = True
        db.session.add(game)
        db.session.commit()  # needed to get a game id for the hashid
        game.set_hashid()
        db.session.commit()
        return redirect(url_for('game.start', hashid=game.hashid))
    return render_template('game/create_X01.html', form=form, opponent_name=opponent_name,
                           title=lazy_gettext('Create Game'))


@bp.route('/')
@bp.route('/<hashid>')
@bp.route('/<hashid>/<theme>')
def start(hashid, theme=None):
    game = Game.query.filter_by(hashid=hashid).first_or_404()
    # check if we found an opponent, logged in users only
    if game.status == 'challenged' and current_user.is_authenticated \
            and current_user.id != game.player1:
        if not game.player2 or game.player2 == current_user.id:
            game.player2 = current_user.id
            game.status = 'started'
            db.session.commit()
            # send message to global chat
            broadcast_new_game(game)
            # signal the waiting player and spectators
            start_game(hashid)

    game_dict = game.as_dict()
    if game.player1:
        game_dict['player1_name'] = get_name_by_id(game.player1)

    if game.opponent_type == 'local':
        game_dict['player2_name'] = lazy_gettext('Local Guest')
    elif game.opponent_type == 'online':
        game_dict['player2_name'] = get_name_by_id(game.player2)
    else:
        # computer game
        if game.bot_id:
            bot = Bot.query.filter_by(id=game.bot_id).first_or_404()
            game_dict['player2_name'] = bot.name
        else:
            game_dict['player2_name'] = 'Trainer ' + game_dict['opponent_type'][8:]

    match_json = json.loads(game.match_json)

    # for player1 and spectators while waiting
    if game.status == 'challenged':
        p2_name = get_name_by_id(game.player2) if game.player2 else None
        return render_template('game/wait_for_opponent.html', game=game_dict, p2_name=p2_name,
                               title=lazy_gettext("Waiting..."))
    # for everyone if the game is completed
    if game.status in ('completed', 'aborted', 'declined'):
        statistics = collect_statistics(game, match_json)
        return render_template('game/X01_completed.html', game=game_dict, match_json=match_json,
                               stats=statistics, title=lazy_gettext('Match overview'))
    # for running games
    else:
        form = ScoreForm()
        chat_form = GameChatmessageForm()
        chat_form_small = GameChatmessageForm()
        caller = current_user.caller if current_user.is_authenticated else 'default'
        cpu_delay = current_user.cpu_delay if current_user.is_authenticated else 0

        user = current_user.id if current_user.is_authenticated else None

        if user == game.player1 or user == game.player2:
            messages = ChatmessageIngame.query.filter_by(game_hashid=game.hashid).order_by(ChatmessageIngame.id.asc()).all()
        else:
            messages = []
        user_names = {}

        for message in messages:
            user_names[message.author] = User.query.with_entities(User.username) \
                .filter_by(id=message.author).first_or_404()[0]

        if theme:
            return render_template('game/X01_stream.html', game=game_dict, form=form,
                                   match_json=match_json, caller=caller, cpu_delay=cpu_delay,
                                   title=lazy_gettext('Stream overlay'),
                                   chat_form=chat_form, chat_form_small=chat_form_small,
                                   messages=messages, user_names=user_names)
        return render_template('game/X01.html', game=game_dict, form=form, match_json=match_json,
                               caller=caller, cpu_delay=cpu_delay, title=lazy_gettext('Live Match'),
                               chat_form=chat_form, chat_form_small=chat_form_small,
                               messages=messages, user_names=user_names)


@bp.route('/validate_score', methods=['POST'])
def validate_score():
    # validating the score input from users
    form = ScoreForm(request.form)
    result = form.validate()
    return jsonify(form.errors)


@bp.route('/decline_challenge/')
@bp.route('/decline_challenge/<id>', methods=['POST'])
def decline_challenge(id):
    id = int(id)
    game = Game.query.filter_by(id=id).first_or_404()
    game.status = "declined"
    game.end = datetime.utcnow()
    db.session.commit()
    return jsonify('success')


@bp.route('/cancel_challenge/<hashid>')
def cancel_challenge(hashid):
    game = Game.query.filter_by(hashid=hashid).first_or_404()
    game.status = "declined"
    game.end = datetime.utcnow()
    db.session.commit()
    return redirect(url_for('generic.lobby'))


@bp.route('/abort_game/')
@bp.route('/abort_game/<hashid>', methods=['POST'])
def abort_game(hashid):
    game = Game.query.filter_by(hashid=hashid).first_or_404()
    game.status = "aborted"
    game.end = datetime.utcnow()
    broadcast_game_aborted(game)
    db.session.commit()
    return redirect(url_for('generic.lobby'))


@bp.route('/calc_all_stats')
def calc_all_stats():
    users = User.query.all()
    for user in users:
        current_stats = calculate_statistics(user.id)
        stats = Stats.query.filter_by(user_id=user.id).first()
        if not stats:
            stats = Stats(user_id=user.id, darts_thrown=current_stats['darts_thrown'],
                          double_thrown=current_stats['double_thrown'], legs_won=current_stats['legs_won'],
                          doubles=current_stats['doubles'], average=current_stats['average'],
                          first9_average=current_stats['first9_average'])
            db.session.add(stats)
        else:
            stats.darts_thrown = stats['darts_thrown']
            stats.double_thrown = stats['double_thrown']
            stats.legs_won = stats['legs_won']
            stats.doubles = stats['doubles']
            stats.average = stats['average']
            stats.first9_average = stats['first9_average']

    db.session.commit()
    return jsonify('success')

