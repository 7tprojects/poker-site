from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO, emit, join_room, leave_room
import hashlib
import random
import secrets
import json
from threading import Timer

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key-change-this'
socketio = SocketIO(app, cors_allowed_origins="*")

# Store active games
games = {}
player_timers = {}

class ProvablyFairDeck:
    """Provably fair card shuffling with cryptographic verification"""
    
    SUITS = ['♠', '♥', '♦', '♣']
    RANKS = ['2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K', 'A']
    
    def __init__(self):
        self.seed = None
        self.seed_hash = None
        self.deck = []
        
    def generate_seed(self):
        """Generate cryptographically secure random seed"""
        self.seed = secrets.token_hex(32)
        self.seed_hash = hashlib.sha256(self.seed.encode()).hexdigest()
        return self.seed_hash
    
    def create_deck(self):
        """Create standard 52-card deck"""
        deck = []
        for suit in self.SUITS:
            for rank in self.RANKS:
                deck.append({'rank': rank, 'suit': suit})
        return deck
    
    def shuffle(self, seed=None):
        """Shuffle deck using Fisher-Yates with seeded random"""
        if seed:
            self.seed = seed
        
        random.seed(self.seed)
        self.deck = self.create_deck()
        n = len(self.deck)
        
        for i in range(n - 1, 0, -1):
            j = random.randint(0, i)
            self.deck[i], self.deck[j] = self.deck[j], self.deck[i]
        
        return self.deck
    
    def verify(self, revealed_seed):
        """Verify that revealed seed matches the hash"""
        return hashlib.sha256(revealed_seed.encode()).hexdigest() == self.seed_hash
    
    def get_cards(self, num):
        """Deal cards from top of deck"""
        if len(self.deck) < num:
            return []
        cards = self.deck[:num]
        self.deck = self.deck[num:]
        return cards


class PokerGame:
    """Texas Hold'em Poker Game Logic with Betting"""
    
    def __init__(self, room_id, small_blind=10, big_blind=20, creator_id=None):
        self.room_id = room_id
        self.creator_id = creator_id
        self.players = {}
        self.player_order = []
        self.deck = ProvablyFairDeck()
        self.community_cards = []
        self.pot = 0
        self.current_bet = 0
        self.state = 'waiting'  # waiting, playing, paused
        self.hand_state = 'none'  # none, preflop, flop, turn, river, showdown
        self.dealer_position = 0
        self.current_player_index = 0
        self.small_blind = small_blind
        self.big_blind = big_blind
        self.last_raiser_index = None
        self.action_timer = 30
        self.auto_deal = True
        self.showdown_timer = None
        self.players_acted = set()  # Track who has acted this betting round
        
    def add_player(self, player_id, player_name):
        """Add player to game"""
        if player_id not in self.players:
            self.players[player_id] = {
                'id': player_id,
                'name': player_name,
                'chips': 1000,
                'bet': 0,
                'hand': [],
                'folded': False,
                'all_in': False,
                'time_remaining': self.action_timer
            }
            self.player_order.append(player_id)
        return self.players[player_id]
    
    def remove_player(self, player_id):
        """Remove player from game"""
        if player_id in self.players:
            del self.players[player_id]
            if player_id in self.player_order:
                self.player_order.remove(player_id)
    
    def get_active_players(self):
        """Get players who haven't folded"""
        return [p for p in self.players.values() if not p['folded']]
    
    def get_players_can_act(self):
        """Get players who can still act (not folded, not all-in)"""
        return [p for p in self.players.values() if not p['folded'] and not p['all_in']]
    
    def post_blinds(self):
        """Post small and big blinds"""
        if len(self.player_order) < 2:
            return False
        
        # Small blind is left of dealer
        sb_index = (self.dealer_position + 1) % len(self.player_order)
        sb_player = self.players[self.player_order[sb_index]]
        
        # Big blind is left of small blind
        bb_index = (self.dealer_position + 2) % len(self.player_order)
        bb_player = self.players[self.player_order[bb_index]]
        
        # Post small blind
        sb_amount = min(self.small_blind, sb_player['chips'])
        sb_player['chips'] -= sb_amount
        sb_player['bet'] = sb_amount
        self.pot += sb_amount
        
        # Post big blind
        bb_amount = min(self.big_blind, bb_player['chips'])
        bb_player['chips'] -= bb_amount
        bb_player['bet'] = bb_amount
        self.pot += bb_amount
        
        self.current_bet = self.big_blind
        
        # First to act is left of big blind
        self.current_player_index = (bb_index + 1) % len(self.player_order)
        self.last_raiser_index = bb_index
        
        # Mark blinds as acted (they posted)
        self.players_acted.add(self.player_order[sb_index])
        self.players_acted.add(self.player_order[bb_index])
        
        return True
    
    def start_hand(self):
        """Start a new hand"""
        if len(self.player_order) < 2:
            return False
        
        if self.state != 'playing':
            return False
        
        # Generate seed and shuffle
        seed_hash = self.deck.generate_seed()
        self.deck.shuffle()
        
        # Reset game state
        self.community_cards = []
        self.pot = 0
        self.current_bet = 0
        self.hand_state = 'preflop'
        self.players_acted = set()
        
        # Reset all players
        for player in self.players.values():
            player['bet'] = 0
            player['folded'] = False
            player['all_in'] = False
            player['hand'] = []
            player['time_remaining'] = self.action_timer
        
        # Deal 2 cards to each player
        for player_id in self.player_order:
            self.players[player_id]['hand'] = self.deck.get_cards(2)
        
        # Post blinds
        self.post_blinds()
        
        return True
    
    def get_current_player(self):
        """Get the player whose turn it is"""
        if not self.player_order or self.hand_state == 'none' or self.hand_state == 'showdown':
            return None
        return self.players[self.player_order[self.current_player_index]]
    
    def check_betting_complete(self):
        """Check if betting round is complete"""
        active_players = self.get_active_players()
        can_act_players = self.get_players_can_act()
        
        # If only one player left, betting is done
        if len(active_players) <= 1:
            return True
        
        # If no one can act (all folded or all-in), betting is done
        if len(can_act_players) == 0:
            return True
        
        # All players who can act must have:
        # 1. Acted at least once this round, AND
        # 2. Matched the current bet
        for player in can_act_players:
            # If player hasn't acted yet, betting not complete
            if player['id'] not in self.players_acted:
                return False
            
            # If player hasn't matched current bet, betting not complete
            if player['bet'] != self.current_bet:
                return False
        
        return True
    
    def advance_action(self):
        """Move to next player or next betting round"""
        active_players = self.get_active_players()
        
        if len(active_players) <= 1:
            # Only one player left, end hand
            self.end_hand()
            return
        
        # Check if betting round is complete
        if self.check_betting_complete():
            self.next_betting_round()
            return
        
        # Move to next active player who can act
        attempts = 0
        max_attempts = len(self.player_order)
        
        while attempts < max_attempts:
            self.current_player_index = (self.current_player_index + 1) % len(self.player_order)
            current_player = self.players[self.player_order[self.current_player_index]]
            
            # Skip folded or all-in players
            if not current_player['folded'] and not current_player['all_in']:
                # Reset timer for new player
                current_player['time_remaining'] = self.action_timer
                return
            
            attempts += 1
        
        # If we've checked everyone and no one can act, end betting round
        self.next_betting_round()
    
    def next_betting_round(self):
        """Move to next betting round"""
        print(f"Moving from {self.hand_state} to next round")
        
        # Reset for next round
        for player in self.players.values():
            player['bet'] = 0
        
        self.current_bet = 0
        self.last_raiser_index = None
        self.players_acted = set()  # Clear who has acted
        
        if self.hand_state == 'preflop':
            self.deal_flop()
        elif self.hand_state == 'flop':
            self.deal_turn()
        elif self.hand_state == 'turn':
            self.deal_river()
        elif self.hand_state == 'river':
            self.end_hand()
    
    def deal_flop(self):
        """Deal the flop (3 community cards)"""
        self.community_cards = self.deck.get_cards(3)
        self.hand_state = 'flop'
        self.set_first_to_act()
        print(f"Flop dealt: {self.community_cards}")
    
    def deal_turn(self):
        """Deal the turn (4th community card)"""
        self.community_cards.extend(self.deck.get_cards(1))
        self.hand_state = 'turn'
        self.set_first_to_act()
        print(f"Turn dealt: {self.community_cards[-1]}")
    
    def deal_river(self):
        """Deal the river (5th community card)"""
        self.community_cards.extend(self.deck.get_cards(1))
        self.hand_state = 'river'
        self.set_first_to_act()
        print(f"River dealt: {self.community_cards[-1]}")
    
    def set_first_to_act(self):
        """Set first active player to act after dealer"""
        self.current_player_index = (self.dealer_position + 1) % len(self.player_order)
        attempts = 0
        while attempts < len(self.player_order):
            player = self.players[self.player_order[self.current_player_index]]
            if not player['folded'] and not player['all_in']:
                player['time_remaining'] = self.action_timer
                return
            self.current_player_index = (self.current_player_index + 1) % len(self.player_order)
            attempts += 1
    
    def determine_winner(self):
        """Determine winner (simplified - just returns active players for now)"""
        # TODO: Implement proper hand evaluation
        # For now, just award to remaining player(s)
        active_players = self.get_active_players()
        
        if len(active_players) == 1:
            return [active_players[0]]
        
        # If multiple players, split pot (simplified)
        return active_players
    
    def end_hand(self):
        """End the hand and award pot"""
        print("Hand ending - going to showdown")
        self.hand_state = 'showdown'
        
        # Determine winner(s)
        winners = self.determine_winner()
        
        if len(winners) == 1:
            # Single winner gets entire pot
            winners[0]['chips'] += self.pot
            print(f"{winners[0]['name']} wins ${self.pot}")
        else:
            # Split pot among winners
            split_amount = self.pot // len(winners)
            for winner in winners:
                winner['chips'] += split_amount
                print(f"{winner['name']} wins ${split_amount} (split pot)")
        
        # Move dealer button
        self.dealer_position = (self.dealer_position + 1) % len(self.player_order)
        
        # Auto-deal next hand after 5 seconds if enabled
        if self.auto_deal and self.state == 'playing':
            socketio.start_background_task(self.schedule_next_hand)
    
    def schedule_next_hand(self):
        """Schedule next hand after showdown"""
        print("Scheduling next hand in 5 seconds...")
        socketio.sleep(5)
        if self.state == 'playing':
            print("Starting new hand")
            self.hand_state = 'none'
            socketio.emit('game_state', self.get_state(), room=self.room_id)
            socketio.sleep(2)
            if self.start_hand():
                socketio.emit('game_state', self.get_state(), room=self.room_id)
    
    def player_fold(self, player_id):
        """Player folds"""
        if player_id in self.players:
            self.players[player_id]['folded'] = True
            self.players_acted.add(player_id)
            print(f"{self.players[player_id]['name']} folds")
            self.advance_action()
            return True
        return False
    
    def player_call(self, player_id):
        """Player calls current bet"""
        if player_id not in self.players:
            return False
        
        player = self.players[player_id]
        call_amount = self.current_bet - player['bet']
        
        if call_amount > player['chips']:
            # All in
            self.pot += player['chips']
            player['bet'] += player['chips']
            player['chips'] = 0
            player['all_in'] = True
            print(f"{player['name']} goes all-in for ${player['bet']}")
        else:
            player['chips'] -= call_amount
            player['bet'] += call_amount
            self.pot += call_amount
            print(f"{player['name']} calls ${call_amount}")
        
        self.players_acted.add(player_id)
        self.advance_action()
        return True
    
    def player_raise(self, player_id, raise_amount):
        """Player raises"""
        if player_id not in self.players:
            return False
        
        player = self.players[player_id]
        total_bet = self.current_bet + raise_amount
        amount_to_add = total_bet - player['bet']
        
        if amount_to_add > player['chips']:
            return False
        
        player['chips'] -= amount_to_add
        player['bet'] = total_bet
        self.pot += amount_to_add
        self.current_bet = total_bet
        
        # Clear players_acted since everyone needs to respond to the raise
        self.players_acted = {player_id}  # Only raiser has acted
        
        print(f"{player['name']} raises to ${total_bet}")
        
        self.advance_action()
        return True
    
    def player_check(self, player_id):
        """Player checks"""
        if player_id not in self.players:
            return False
        
        player = self.players[player_id]
        
        # Can only check if bet matches current bet
        if player['bet'] != self.current_bet:
            return False
        
        print(f"{player['name']} checks")
        self.players_acted.add(player_id)
        self.advance_action()
        return True
    
    def get_state(self):
        """Get current game state"""
        current_player = self.get_current_player()
        return {
            'room_id': self.room_id,
            'creator_id': self.creator_id,
            'players': [self.players[pid] for pid in self.player_order],
            'community_cards': self.community_cards,
            'pot': self.pot,
            'current_bet': self.current_bet,
            'state': self.state,
            'hand_state': self.hand_state,
            'seed_hash': self.deck.seed_hash,
            'current_player_id': current_player['id'] if current_player else None,
            'dealer_position': self.dealer_position,
            'small_blind': self.small_blind,
            'big_blind': self.big_blind,
            'action_timer': self.action_timer,
            'auto_deal': self.auto_deal
        }

# class Showdown:


@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('connect')
def handle_connect():
    print(f'Client connected: {request.sid}')

@socketio.on('disconnect')
def handle_disconnect():
    print(f'Client disconnected: {request.sid}')
    # Cancel any active timers for this player
    if request.sid in player_timers:
        player_timers[request.sid].cancel()
        del player_timers[request.sid]
    
    for game in games.values():
        if request.sid in game.players:
            game.remove_player(request.sid)
            emit('game_state', game.get_state(), room=game.room_id)

@socketio.on('create_room')
def handle_create_room(data):
    room_id = data.get('room_id', 'default')
    player_name = data.get('player_name', 'Anonymous')
    small_blind = int(data.get('small_blind', 10))
    big_blind = int(data.get('big_blind', 20))
    
    join_room(room_id)
    
    if room_id not in games:
        games[room_id] = PokerGame(room_id, small_blind, big_blind, creator_id=request.sid)
    
    game = games[room_id]
    game.add_player(request.sid, player_name)
    
    emit('joined', {'room_id': room_id, 'player_id': request.sid}, room=request.sid)
    emit('game_state', game.get_state(), room=room_id)

@socketio.on('join_game')
def handle_join_game(data):
    room_id = data.get('room_id', 'default')
    player_name = data.get('player_name', 'Anonymous')
    
    if room_id not in games:
        emit('error', {'message': 'Room does not exist'}, room=request.sid)
        return
    
    join_room(room_id)
    game = games[room_id]
    game.add_player(request.sid, player_name)
    
    emit('joined', {'room_id': room_id, 'player_id': request.sid}, room=request.sid)
    emit('game_state', game.get_state(), room=room_id)

@socketio.on('start_game')
def handle_start_game(data):
    room_id = data.get('room_id')
    if room_id in games:
        game = games[room_id]
        
        # Only creator can start
        if request.sid != game.creator_id:
            emit('error', {'message': 'Only room creator can start the game'}, room=request.sid)
            return
        
        game.state = 'playing'
        if game.start_hand():
            emit('game_state', game.get_state(), room=room_id)

@socketio.on('pause_game')
def handle_pause_game(data):
    room_id = data.get('room_id')
    if room_id in games:
        game = games[room_id]
        
        # Only creator can pause
        if request.sid != game.creator_id:
            emit('error', {'message': 'Only room creator can pause the game'}, room=request.sid)
            return
        
        game.state = 'paused'
        emit('game_state', game.get_state(), room=room_id)

@socketio.on('player_action')
def handle_player_action(data):
    room_id = data.get('room_id')
    action = data.get('action')
    player_id = request.sid
    
    if room_id not in games:
        return
    
    game = games[room_id]
    
    # Verify it's this player's turn
    current_player = game.get_current_player()
    if not current_player or current_player['id'] != player_id:
        emit('error', {'message': 'Not your turn'}, room=request.sid)
        return
    
    if action == 'fold':
        game.player_fold(player_id)
    elif action == 'call':
        game.player_call(player_id)
    elif action == 'check':
        if not game.player_check(player_id):
            emit('error', {'message': 'Cannot check'}, room=request.sid)
            return
    elif action == 'raise':
        raise_amount = int(data.get('raise_amount', 0))
        if not game.player_raise(player_id, raise_amount):
            emit('error', {'message': 'Invalid raise'}, room=request.sid)
            return
    
    emit('game_state', game.get_state(), room=room_id)

@socketio.on('verify_seed')
def handle_verify_seed(data):
    room_id = data.get('room_id')
    if room_id in games:
        game = games[room_id]
        emit('seed_revealed', {
            'seed': game.deck.seed,
            'seed_hash': game.deck.seed_hash
        }, room=request.sid)

if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)