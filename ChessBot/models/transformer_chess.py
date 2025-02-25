import tiktoken
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader
from omegaconf import OmegaConf, open_dict



class EncoderBlock(nn.Module):
    
    def __init__(self, config):
        super().__init__()
        self.config = config
        self.attn = nn.MultiheadAttention(config.embed_dim, config.num_heads, config.dropout)
        self.norm1 = nn.LayerNorm(config.embed_dim)
        self.ff = nn.Sequential(
            nn.Linear(config.embed_dim, config.ff_dim),
            nn.ReLU(),
            nn.Linear(config.ff_dim, config.embed_dim)
        )
        self.norm2 = nn.LayerNorm(config.embed_dim)
        self.dropout = nn.Dropout(config.dropout)

    def forward(self, x):
        '''
        Forward Prop
        
        Args:
            x (torch.FloatTensor): The input tensor, of size (N, Board Status Length, embed_dim).
        
        Returns:
            torch.FloatTensor: The output tensor, of size (N, Board Status Length, embed_dim).
        '''
        # Self Attention
        x = x.permute(1, 0, 2)
        x = self.attn(x, x, x)[0]
        x = self.dropout(x)
        x = x.permute(1, 0, 2)

        # Normalize
        x = self.norm1(x)

        # Feed Forward
        x = self.ff(x)
        x = self.dropout(x)

        # Normalize
        x = self.norm2(x)

        return x
       

class EncoderOnlyTransformer(nn.Module):
    def __init__(self, config):
        # for chess 
        super().__init__()
        self.config = config
        # Embeddings maybe have one giant embedding for all the pieces
        self.board_embed = nn.Embedding(config.board_vocab_size, config.embed_dim)
        self.positional_embed = nn.Embedding(config.pos_size, config.embed_dim)
        self.turn_embed = nn.Embedding(config.turn_size, config.embed_dim)
        self.white_kingside_castling_rights_embed = nn.Embedding(config.castling_size, config.embed_dim)
        self.white_queenside_castling_rights_embed = nn.Embedding(config.castling_size, config.embed_dim)
        self.black_kingside_castling_rights_embed = nn.Embedding(config.castling_size, config.embed_dim)
        self.black_queenside_castling_rights_embed = nn.Embedding(config.castling_size, config.embed_dim)

        self.dropout = nn.Dropout(config.dropout)

        self.transformer_blocks = nn.Sequential(*[
            EncoderBlock(config) for _ in range(config.num_layers)
        ])

        self.norm = nn.LayerNorm(config.embed_dim)
        self.moves_head = nn.Linear(config.embed_dim * config.pos_size, config.moves_vocab_size)
        self.winrate_head = nn.Sequential(
            nn.Linear(config.embed_dim * config.pos_size, 1), 
            nn.Sigmoid()
        )

    
    def init_weights(self):
        #TODO
        pass
        

    def forward(self, batch : dict):
        '''
        Forward Prop
        
        Args:
            batch (dict): A single batch, containing the following keys:

                turns (torch.LongTensor): The current turn (w/b), of
                size (N, 1).

                white_kingside_castling_rights (torch.LongTensor):
                Whether white can castle kingside, of size (N, 1).

                white_queenside_castling_rights (torch.LongTensor):
                Whether white can castle queenside, of size (N, 1).

                black_kingside_castling_rights (torch.LongTensor):
                Whether black can castle kingside, of size (N, 1).

                black_queenside_castling_rights (torch.LongTensor):
                Whether black can castle queenside, of size (N, 1).

                board_positions (torch.LongTensor): The current board
                positions, of size (N, 64).

                moves (torch.LongTensor): The move sequences, of size
                (N, n_moves).

                lengths (torch.LongTensor): The true lengths of the move
                sequences, not including <move> and <pad> tokens, of
                size (N, 1).

        Returns:
            dict: A dictionary containing the following keys:

                move (torch.FloatTensor): The predicted move distribution,
                of size (N, n_moves).

                winrate (torch.FloatTensor): The predicted winrate, of
                size (N, 1).
        '''
        # Create Embeddings
        embeddings = torch.cat([ 
            self.board_embed(batch["board_positions"]),
            self.turn_embed(batch["turns"]),
            self.white_kingside_castling_rights_embed(batch["white_kingside_castling_rights"]),
            self.white_queenside_castling_rights_embed(batch["white_queenside_castling_rights"]),
            self.black_kingside_castling_rights_embed(batch["black_kingside_castling_rights"]),
            self.black_queenside_castling_rights_embed(batch["black_queenside_castling_rights"])
        ], dim=1)

        boards = embeddings + self.positional_embed.weight.unsqueeze(0)
        # size (N, Board Status Length, embed_dim)
        boards = self.dropout(boards)

        # Transformer Blocks
        for block in self.transformer_blocks:
            boards = block(boards)

        # Normalize
        boards = self.norm(boards)
        batch_size = boards.size(0)
        boards = boards.view(batch_size, -1)

        # Heads
        moves = self.moves_head(boards)
        # shape of moves is (N, n_moves)
        winrate = self.winrate_head(boards) 
        # shape of winrate is (N, 1)
        return {
            "move": moves,
            "winrate": winrate
        }
