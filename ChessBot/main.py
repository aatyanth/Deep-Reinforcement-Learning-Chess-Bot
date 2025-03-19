import os
import hydra
import torch
import datetime
import chess
import chess.pgn
import chess.engine
from omegaconf import DictConfig, OmegaConf
import argparse
import tqdm
import random

from models.transformer_chess import EncoderOnlyTransformer
from utils.utils import play_game, save_game
from utils.mcts import get_best_move_mcts, MCTSNode


def load_model(config_path, checkpoint_path, device):
    """
    Load the chess transformer model from a checkpoint.
    
    Args:
        config_path: Path to the config file
        checkpoint_path: Path to the model checkpoint
        device: Device to run the model on
        
    Returns:
        Loaded model
    """
    # Load config
    config = OmegaConf.load(config_path)
    print("Loaded configuration:")
    print(OmegaConf.to_yaml(config))
    
    # Create model
    model = EncoderOnlyTransformer(config.model).to(device)
    
    # Load checkpoint
    if os.path.exists(checkpoint_path):
        try:
            print(f"Loading model from {checkpoint_path}")
            checkpoint = torch.load(checkpoint_path, map_location=device)
            model.load_state_dict(checkpoint['model_state_dict'])
            print(f"Successfully loaded model (epoch {checkpoint.get('epoch', 'unknown')})")
            
            # Set model to evaluation mode
            model.eval()
            return model
        except Exception as e:
            print(f"Error loading model: {e}")
            raise e
    else:
        print(f"No model checkpoint found at {checkpoint_path}")
        raise FileNotFoundError(f"Model checkpoint not found: {checkpoint_path}")


def load_engine(engine_path, config=None):
    """
    Load the Stockfish engine.
    
    Args:
        engine_path: Path to the Stockfish executable
        config: Optional config for engine settings
        
    Returns:
        Loaded engine
    """
    try:
        engine = chess.engine.SimpleEngine.popen_uci(engine_path)
        print(f"Successfully loaded engine from {engine_path}")
        return engine
    except Exception as e:
        print(f"Error loading engine: {e}")
        raise e


def warm_up(model, device="cuda"):
    """
    Warm up the model by running a few forward passes.
    
    Args:
        model: The model to warm up
        device: Device to run the model on
    """
    print("Warming up model...")
    # Create a dummy board
    board = chess.Board()
    
    # Create model input
    with torch.no_grad():
        # Convert board to model input format
        board_tensor = torch.zeros(1, 64, dtype=torch.long)
        
        # Map pieces to indices
        piece_to_idx = {
            'P': 1, 'N': 2, 'B': 3, 'R': 4, 'Q': 5, 'K': 6,  # White pieces
            'p': 7, 'n': 8, 'b': 9, 'r': 10, 'q': 11, 'k': 12  # Black pieces
        }
        
        # Fill board tensor
        for square in chess.SQUARES:
            piece = board.piece_at(square)
            if piece:
                board_tensor[0, square] = piece_to_idx[piece.symbol()]
        
        # Turn
        turn_tensor = torch.tensor([1 if board.turn == chess.WHITE else 0], dtype=torch.long)
        
        # Castling rights
        w_kingside = torch.tensor([1 if board.has_kingside_castling_rights(chess.WHITE) else 0], dtype=torch.long)
        w_queenside = torch.tensor([1 if board.has_queenside_castling_rights(chess.WHITE) else 0], dtype=torch.long)
        b_kingside = torch.tensor([1 if board.has_kingside_castling_rights(chess.BLACK) else 0], dtype=torch.long)
        b_queenside = torch.tensor([1 if board.has_queenside_castling_rights(chess.BLACK) else 0], dtype=torch.long)
        
        # Create batch
        batch = {
            "board_positions": board_tensor.to(device),
            "turns": turn_tensor.to(device),
            "white_kingside_castling_rights": w_kingside.to(device),
            "white_queenside_castling_rights": w_queenside.to(device),
            "black_kingside_castling_rights": b_kingside.to(device),
            "black_queenside_castling_rights": b_queenside.to(device)
        }
        
        # Run forward pass a few times
        for _ in range(3):
            _ = model(batch)
    
    print("Model warm-up complete")


def model_v_engine(model, engine, model_color, skill_level=6, simulations=800, 
                   temperature=0.2, rounds=10, time_limit=0.1, depth_limit=5, 
                   output_dir="evaluation_games", device="cuda"):
    """
    Play games between the model and Stockfish engine to evaluate model strength.
    
    Args:
        model: The chess transformer model
        engine: The Stockfish engine
        model_color: "white", "black", or "both" to specify which color the model plays
        skill_level: Stockfish skill level (0-20)
        simulations: Number of MCTS simulations per move
        temperature: Temperature for move selection
        rounds: Number of games to play
        time_limit: Time limit for Stockfish per move in seconds
        depth_limit: Depth limit for Stockfish search
        output_dir: Directory to save PGN game records
        device: Device to run the model on
        
    Returns:
        Tuple of (wins, losses, draws, pgns)
    """
    # Create output directory
    os.makedirs(output_dir, exist_ok=True)
    
    # Set engine parameters
    engine.configure({"Skill Level": skill_level})
    
    wins, losses, draws = 0, 0, 0
    pgns = []
    
    # Define colors for each round
    colors = []
    if model_color.lower() == "white":
        colors = ["white"] * rounds
    elif model_color.lower() == "black":
        colors = ["black"] * rounds
    else:  # both
        colors = ["white" if i % 2 == 0 else "black" for i in range(rounds)]
    
    # Play rounds
    for round_num in tqdm.tqdm(range(rounds), desc="Playing games"):
        color = colors[round_num]
        model_plays_white = color == "white"
        
        # Initialize game and board
        board = chess.Board()
        game = chess.pgn.Game()
        
        # Set game headers
        timestamp = datetime.datetime.now().strftime("%Y.%m.%d")
        game.headers["Event"] = f"Model vs Stockfish (Skill Level {skill_level})"
        game.headers["Site"] = "Evaluation"
        game.headers["Date"] = timestamp
        game.headers["Round"] = str(round_num + 1)
        game.headers["White"] = "Model" if model_plays_white else f"Stockfish (Skill Level {skill_level})"
        game.headers["Black"] = f"Stockfish (Skill Level {skill_level})" if model_plays_white else "Model"
        
        # Play the game
        node = game
        move_count = 0
        
        while not board.is_game_over() and move_count < 200:  # Limit to 200 moves max
            is_model_turn = (board.turn == chess.WHITE and model_plays_white) or \
                           (board.turn == chess.BLACK and not model_plays_white)
            
            # Get move
            if is_model_turn:
                # Model makes a move using MCTS
                move = get_best_move_mcts(
                    board=board,
                    model=model,
                    device=device,
                    temperature=temperature,
                    simulations=simulations
                )
            else:
                # Stockfish makes a move
                result = engine.play(
                    board,
                    chess.engine.Limit(time=time_limit, depth=depth_limit)
                )
                move = result.move
            
            # Make the move
            if move:
                board.push(move)
                node = node.add_variation(move)
                move_count += 1
            else:
                # No legal moves or something went wrong
                break
        
        # Determine result
        result = board.result()
        
        if result == "1-0":
            if model_plays_white:
                wins += 1
            else:
                losses += 1
        elif result == "0-1":
            if model_plays_white:
                losses += 1
            else:
                wins += 1
        else:
            draws += 1
        
        game.headers["Result"] = result
        pgns.append(game)
        
        # Save game
        save_path = os.path.join(output_dir, f"game_{round_num+1}_{color}_{result}.pgn")
        with open(save_path, "w") as f:
            print(game, file=f)
    
    return wins, losses, draws, pgns


def write_pgns(pgns, filename):
    """
    Write a collection of PGN games to a file.
    
    Args:
        pgns: List of chess.pgn.Game objects
        filename: Path to save the PGN file
    """
    with open(filename, "w") as f:
        for i, game in enumerate(pgns):
            print(game, file=f, end="\n\n")
            if i < len(pgns) - 1:
                print("", file=f)
    
    print(f"Saved {len(pgns)} games to {filename}")


def evaluate_at_skills(model, stockfish_path, config_path, checkpoint_path, 
                       output_dir="evaluation_results", device="cuda"):
    """
    Evaluate model against Stockfish at different skill levels.
    
    Args:
        model: The chess transformer model
        stockfish_path: Path to Stockfish executable
        config_path: Path to the config file
        checkpoint_path: Path to the model checkpoint
        output_dir: Directory to save evaluation results
        device: Device to run the model on
    """
    os.makedirs(output_dir, exist_ok=True)
    
    # Define skill levels to test
    skill_levels = [1, 3, 5, 7, 10]
    rounds_per_level = 10
    
    # Results summary
    results = {}
    
    # Load engine
    engine = load_engine(stockfish_path)
    
    try:
        for skill in skill_levels:
            print(f"\n=== Testing against Stockfish Skill Level {skill} ===")
            
            # Play as white
            print("Model playing as White")
            w_wins, w_losses, w_draws, w_pgns = model_v_engine(
                model=model,
                engine=engine,
                model_color="white",
                skill_level=skill,
                rounds=rounds_per_level,
                output_dir=os.path.join(output_dir, f"skill_{skill}"),
                device=device
            )
            
            # Play as black
            print("Model playing as Black")
            b_wins, b_losses, b_draws, b_pgns = model_v_engine(
                model=model,
                engine=engine,
                model_color="black",
                skill_level=skill,
                rounds=rounds_per_level,
                output_dir=os.path.join(output_dir, f"skill_{skill}"),
                device=device
            )
            
            # Save all PGNs for this skill level
            all_pgns = w_pgns + b_pgns
            pgn_path = os.path.join(output_dir, f"skill_{skill}_games.pgn")
            write_pgns(all_pgns, pgn_path)
            
            # Calculate total results
            total_wins = w_wins + b_wins
            total_losses = w_losses + b_losses
            total_draws = w_draws + b_draws
            total_games = total_wins + total_losses + total_draws
            win_rate = total_wins / total_games * 100 if total_games > 0 else 0
            
            # Store results
            results[skill] = {
                "wins": total_wins,
                "losses": total_losses,
                "draws": total_draws,
                "win_rate": win_rate,
                "white_results": {"wins": w_wins, "losses": w_losses, "draws": w_draws},
                "black_results": {"wins": b_wins, "losses": b_losses, "draws": b_draws}
            }
            
            print(f"Skill Level {skill} Summary:")
            print(f"Wins: {total_wins}, Losses: {total_losses}, Draws: {total_draws}")
            print(f"Win Rate: {win_rate:.2f}%")
            
    finally:
        # Always close the engine
        engine.quit()
    
    # Save summary report
    report_path = os.path.join(output_dir, "evaluation_summary.txt")
    with open(report_path, "w") as f:
        f.write("Chess Transformer Model Evaluation Report\n")
        f.write("=======================================\n\n")
        f.write(f"Model: {checkpoint_path}\n")
        f.write(f"Date: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        
        f.write("Results by Stockfish Skill Level:\n")
        for skill in skill_levels:
            r = results[skill]
            f.write(f"\nSkill Level {skill}:\n")
            f.write(f"  Total Games: {r['wins'] + r['losses'] + r['draws']}\n")
            f.write(f"  Wins: {r['wins']}, Losses: {r['losses']}, Draws: {r['draws']}\n")
            f.write(f"  Win Rate: {r['win_rate']:.2f}%\n")
            f.write(f"  As White: {r['white_results']['wins']}-{r['white_results']['losses']}-{r['white_results']['draws']}\n")
            f.write(f"  As Black: {r['black_results']['wins']}-{r['black_results']['losses']}-{r['black_results']['draws']}\n")
    
    print(f"\nEvaluation complete. Results saved to {report_path}")
    return results


def main():
    parser = argparse.ArgumentParser(description="Evaluate Chess Transformer against Stockfish")
    parser.add_argument("--config", type=str, default="configs/default.yaml", help="Path to config file")
    parser.add_argument("--checkpoint", type=str, default="checkpoints/latest_model.pt", help="Path to model checkpoint")
    parser.add_argument("--stockfish", type=str, required=True, help="Path to Stockfish executable")
    parser.add_argument("--output_dir", type=str, default="evaluation_results", help="Directory to save results")
    parser.add_argument("--device", type=str, default="cuda" if torch.cuda.is_available() else "cpu", help="Device to run on (cuda/cpu)")
    parser.add_argument("--skill", type=int, default=None, help="Specific Stockfish skill level to evaluate against (1-20)")
    parser.add_argument("--rounds", type=int, default=10, help="Number of games per configuration")
    parser.add_argument("--model_color", type=str, default="both", choices=["white", "black", "both"], help="Color for the model to play")
    
    args = parser.parse_args()
    
    # Set device
    device = torch.device(args.device)
    print(f"Using device: {device}")
    
    # Load model
    model = load_model(args.config, args.checkpoint, device)
    
    # Warm up model
    warm_up(model, device)
    
    if args.skill is not None:
        # Evaluate against specific skill level
        print(f"Evaluating against Stockfish at skill level {args.skill}")
        engine = load_engine(args.stockfish)
        
        try:
            wins, losses, draws, pgns = model_v_engine(
                model=model,
                engine=engine,
                model_color=args.model_color,
                skill_level=args.skill,
                rounds=args.rounds,
                output_dir=args.output_dir,
                device=device
            )
            
            # Save PGNs
            pgn_path = os.path.join(args.output_dir, f"skill_{args.skill}_games.pgn")
            write_pgns(pgns, pgn_path)
            
            print("\nEvaluation results:")
            print(f"Wins: {wins}")
            print(f"Losses: {losses}")
            print(f"Draws: {draws}")
            if wins + losses + draws > 0:
                win_rate = wins / (wins + losses + draws) * 100
                print(f"Win rate: {win_rate:.2f}%")
        finally:
            engine.quit()
    else:
        # Evaluate against multiple skill levels
        results = evaluate_at_skills(
            model=model,
            stockfish_path=args.stockfish,
            config_path=args.config,
            checkpoint_path=args.checkpoint,
            output_dir=args.output_dir,
            device=device
        )


if __name__ == "__main__":
    main()