import os
import time
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

MONGO_URI = os.getenv('MONGO_URI')

class Mongo:
    def __init__(self):
        self.client = MongoClient(MONGO_URI)
        self.database = self.client.leverage_bot
        self.licenses_collection = self.database.licenses
        self.admins_collection = self.database.admins
        # Coleção para configurações de usuário (corretora, login, etc.)
        self.user_configs = self.database.user_configs
        # Coleção para estado da estratégia (Grid Hedge)
        self.strategy_states = self.database.strategy_states
        # Coleção para configurações da estratégia por usuário (NOVA)
        self.strategy_configs = self.database.strategy_configs

    # ========== LICENÇAS ==========
    def set_license(self, telegram_id: int, days: int):
        expiration = int(time.time()) + (days * 86400)
        self.licenses_collection.update_one(
            {'telegram_id': telegram_id},
            {'$set': {
                'expiration': expiration,
                'updated_at': int(time.time())
            }},
            upsert=True
        )

    def get_license(self, telegram_id: int):
        doc = self.licenses_collection.find_one({'telegram_id': telegram_id})
        if doc:
            return doc.get('expiration')
        return None

    def is_license_valid(self, telegram_id: int):
        exp = self.get_license(telegram_id)
        if exp is None:
            return False
        return exp > int(time.time())

    # ========== ADMINISTRADORES ==========
    def add_admin(self, telegram_id: int):
        self.admins_collection.update_one(
            {'telegram_id': telegram_id},
            {'$set': {'telegram_id': telegram_id}},
            upsert=True
        )

    def remove_admin(self, telegram_id: int):
        self.admins_collection.delete_one({'telegram_id': telegram_id})

    def get_admins(self):
        docs = self.admins_collection.find({}, {'telegram_id': 1, '_id': 0})
        admin_ids = [doc['telegram_id'] for doc in docs]
        # Adiciona o admin fixo do .env (se não estiver na lista)
        ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))
        if ADMIN_ID and ADMIN_ID not in admin_ids:
            admin_ids.append(ADMIN_ID)
        return admin_ids

    def is_admin(self, telegram_id: int):
        ADMIN_ID = int(os.getenv('ADMIN_ID', '0'))
        if telegram_id == ADMIN_ID:
            return True
        return self.admins_collection.find_one({'telegram_id': telegram_id}) is not None

    # ========== CONFIGURAÇÃO DA CONTA ==========
    def set_user_config(self, telegram_id: int, config: dict):
        """Salva a configuração da conta do usuário (corretora, tipo, servidor)"""
        self.user_configs.update_one(
            {'telegram_id': telegram_id},
            {'$set': config},
            upsert=True
        )

    def get_user_config(self, telegram_id: int):
        """Recupera a configuração da conta do usuário"""
        doc = self.user_configs.find_one({'telegram_id': telegram_id})
        if doc:
            doc.pop('_id', None)
            return doc
        # Configuração padrão
        return {
            'corretora': 'EXNESS',
            'tipo_conta': 'DEMO',
            'servidor': 'Exness-MT5',
            'conta_cents': False,
            'login_mt5': None,
            'senha_mt5': None
        }

    def update_user_config(self, telegram_id: int, key: str, value):
        """Atualiza um campo específico da configuração"""
        self.user_configs.update_one(
            {'telegram_id': telegram_id},
            {'$set': {key: value}},
            upsert=True
        )

    # ========== PERSISTÊNCIA DA ESTRATÉGIA (Grid Hedge) ==========
    def save_strategy_state(self, user_id: int, state: dict):
        """
        Salva o estado atual da estratégia Grid Hedge para o usuário.
        Isso permite recuperar o estado após reinício do bot.
        """
        self.strategy_states.update_one(
            {'user_id': user_id},
            {'$set': {'state': state, 'updated_at': int(time.time())}},
            upsert=True
        )

    def get_strategy_state(self, user_id: int):
        """
        Recupera o estado salvo da estratégia para o usuário.
        Retorna None se não houver estado salvo.
        """
        doc = self.strategy_states.find_one({'user_id': user_id})
        if doc:
            return doc.get('state', {})
        return None

    # ========== CONFIGURAÇÕES DA ESTRATÉGIA (por usuário) ==========
    def get_strategy_config(self, user_id: int):
        """
        Recupera as configurações da estratégia para o usuário.
        Se não existir, retorna os valores padrão.
        """
        doc = self.strategy_configs.find_one({'user_id': user_id})
        if doc:
            doc.pop('_id', None)
            return doc
        # Configuração padrão (valores iniciais)
        return {
            'user_id': user_id,
            'start_lot': 0.01,
            'increment_lot': 0.01,
            'grid_distance_points': 20000,
            'trailing_activate_usd': 2.50,
            'trailing_start_distance': 2.00,
            'total_profit_to_close': 10.0,
            'daily_target_profit': 100.0,
            'stop_open_position_lot': 10.0,
            'symbol': 'EURUSD'
        }

    def update_strategy_config(self, user_id: int, key: str, value):
        """
        Atualiza uma configuração específica da estratégia para o usuário.
        """
        self.strategy_configs.update_one(
            {'user_id': user_id},
            {'$set': {key: value}},
            upsert=True
        )

    def save_strategy_config(self, user_id: int, config: dict):
        """
        Salva todas as configurações da estratégia para o usuário de uma vez.
        """
        config['user_id'] = user_id
        self.strategy_configs.update_one(
            {'user_id': user_id},
            {'$set': config},
            upsert=True
        )

    def close(self):
        self.client.close()