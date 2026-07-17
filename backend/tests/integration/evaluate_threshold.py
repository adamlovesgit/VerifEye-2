"""Evaluate a provisional cosine threshold against a labeled embedding set."""

import argparse
import json
import numpy as np


def main():
    parser=argparse.ArgumentParser(description="Report genuine/impostor errors for an embedding golden set.")
    parser.add_argument("dataset", help="NPZ containing embeddings [N,D] and labels [N]")
    parser.add_argument("--threshold", type=float, default=.40)
    args=parser.parse_args(); data=np.load(args.dataset,allow_pickle=False)
    embeddings=np.asarray(data["embeddings"],np.float32); labels=np.asarray(data["labels"])
    embeddings/=np.linalg.norm(embeddings,axis=1,keepdims=True).clip(min=1e-12)
    genuine=[]; impostor=[]
    for left in range(len(labels)):
        for right in range(left+1,len(labels)):
            score=float(embeddings[left]@embeddings[right]); (genuine if labels[left]==labels[right] else impostor).append(score)
    false_rejects=sum(score<args.threshold for score in genuine); false_accepts=sum(score>=args.threshold for score in impostor)
    report={"threshold":args.threshold,"genuinePairs":len(genuine),"impostorPairs":len(impostor),
            "falseRejectRate":false_rejects/len(genuine) if genuine else None,
            "falseAcceptRate":false_accepts/len(impostor) if impostor else None,
            "genuineRange":[min(genuine),max(genuine)] if genuine else None,"impostorRange":[min(impostor),max(impostor)] if impostor else None}
    print(json.dumps(report,indent=2))

if __name__=="__main__": main()
