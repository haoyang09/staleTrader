import sys
import os
import time
import json

from pyspark.conf import SparkConf
from pyspark.context import SparkContext
from pyspark.sql import SQLContext
from pyspark.sql.functions import udf, col
import redis

from pyspark.sql.types import IntegerType, ArrayType, StringType

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/config")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/lib")
import config
import util

from MinHash import MinHash
from LSH import LSH
from CustomMinHashLSH import CustomMinHashLSH


#==========================================
# Computes MinHashes, LSHes for all in DataFrame
def compute_minhash_lsh(df, mh, lsh):
    # Compute MinHash/LSH hashes for input questions
    if (config.LOG_DEBUG): print("[BATCH]: Calculating MinHash hashes and LSH hashes...")
    calc_min_hash = udf(lambda x: list(map(lambda x: int(x), mh.calc_min_hash_signature(x))), ArrayType(IntegerType()))
    calc_lsh_hash = udf(lambda x: list(map(lambda x: int(x), lsh.find_lsh_buckets(x))), ArrayType(IntegerType()))

    df = df.withColumn("min_hash", calc_min_hash("text_body_stemmed"))
    df = df.withColumn("lsh_hash", calc_lsh_hash("min_hash"))

    df.foreachPartition(store_lsh_redis)


# Store question data
def store_lsh_redis(rdd):
    rdb = redis.StrictRedis(config.REDIS_SERVER, port=6379, db=0)
    for q in rdd:
        for tag in q.tag_company:
            q_json = json.dumps({"id": q.id, "headline": q.headline, "min_hash": q.min_hash, "lsh_hash": q.lsh_hash, "timestamp": q.display_timestamp })
            rdb.zadd("lsh:{0}".format(tag), q.display_timestamp, q_json)
            rdb.sadd("lsh_keys", "lsh:{0}".format(tag))


# Store duplicate candidates in Redis
def store_dup_cand_redis(rdd):
    rdb = redis.StrictRedis(config.REDIS_SERVER, port=6379, db=0)
    for cand in rdd:
        cand_reformatted = (cand.q1_id, cand.q1_headline, cand.q2_id, cand.q2_headline, cand.q1_timestamp, cand.q2_timestamp)
        # Store by time
        rdb.zadd("dup_cand", cand.mh_js, cand_reformatted)


# Compares LSH signatures, MinHash signature, and find duplicate candidates
def find_dup_cands_within_tags(mh, lsh):
    rdb = redis.StrictRedis(config.REDIS_SERVER, port=6379, db=0)

    if config.LOG_DEBUG: print("find duplicate within tags...")
    # Fetch all tags from lsh_keys set
    for lsh_key in rdb.sscan_iter("lsh_keys", match="*", count=500):
        tag = lsh_key.replace("lsh:", "")
        tq_table_size = rdb.zcard("lsh:{0}".format(tag))

        # Fetch all news. ideally, sort the news by timestamp, and get within a range of timestamps
        tq = rdb.zrangebyscore("lsh:{0}".format(tag), "-inf", "+inf", withscores=False)
        if len(tq) < 2: continue
        if config.LOG_DEBUG: print("tag {0}: {1} news".format(tag, len(tq)))

        tq_df = sql_context.read.json(sc.parallelize(tq))
        find_dup_cands_lsh(tq_df, lsh)


def find_dup_cands_lsh(df, lsh):
    # broadcase the data

    # find set of news ids which have at least one common lsh bucket
    _candidates_with_common_bucket = df.select(col('lsh_hash'), col('id')).rdd.flatMap(
        lambda x: (((hash, band), [x[1]]) for band, hash in enumerate(x[0]))).reduceByKey(
        lambda a, b: util._extend(a,b)).map(lambda x: x[1]).filter(lambda x: len(x)>1).distinct()

    # _candidates_with_common_bucket = df.select(col('id'), col('headline'), col('min_hash'), col('lsh_hash')).rdd.flatMap(
    #     lambda x: (((hash, band), [(x[0], x[1], x[2])]) for band, hash in enumerate(x[3]))).reduceByKey(
    #     lambda a, b: _extend(a,b)).map(lambda x: x[1]).filter(lambda x: len(x)>1).distinct()

    rdd_dataset = _candidates_with_common_bucket.map(lambda candiate_sets: lsh.get_jaccard_similarity(candidate_sets))
    _similar_sets_dict = rdd_dataset.flatMap(lambda x: x.items()).reduceByKey(lambda acc, val: lsh.merge_result(acc, val)).collectAsMap()

    return _similar_sets_dict

    # calculate the number of common buckets each candidate pair has
    # candidate list should be those which have at least certain amount of common buckets
    # calculate the jaccard_similarity for all candidate pairs
    # filter candidate pairs whose jaccard_similarity is above certain threshold

    """
    _rdd_similar_set_candidate_list = _rdd_dataset.map(lambda x: LSH.get_set_signatures(x, _row_list)).flatMap(lambda x:
        ((x[i][0], x[i][1]) for i in range(len(x)))).groupByKey().map(lambda x: tuple(x[1])).filter(lambda x: len(x)>1).distinct()
    if DEBUG: print ('LSH.execute=>_rdd_similar_set_candidate_list =%s'%(_rdd_similar_set_candidate_list.collect()))

    rdd_dataset = _rdd_similar_set_candidate_list.map(lambda candidate_sets: LSH.get_jaccard_similarity(_dataset, candidate_sets))
    _similar_sets_dict = rdd_dataset.flatMap(lambda x: x.items()).reduceByKey(lambda acc, val: LSH.merge_result(acc, val)).collectAsMap()
    if DEBUG: print('LSH.execute=>_similar_sets_dict2=%s'%(_similar_sets_dict))
    return _similar_sets_dict
    """

    """
    ### this is a major performance limiting step which should be optimized
    find_lsh_sim = udf(lambda x, y: lsh.common_bands_count(x, y), IntegerType())
    lsh_sim_df = tq_df.alias("q1").join(tq_df.alias("q2"),
        [col("q1.timestamp") < col("q2.timestamp"),
        col("q2.timestamp") - col("q1.timestamp") < config.TIME_WINDOW]).select(
        col("q1.id").alias("q1_id"),
        col("q2.id").alias("q2_id"),
        col("q1.min_hash").alias("q1_min_hash"),
        col("q2.min_hash").alias("q2_min_hash"),
        col("q1.headline").alias("q1_headline"),
        col("q2.headline").alias("q2_headline"),
        col("q1.timestamp").alias("q1_timestamp"),
        col("q2.timestamp").alias("q2_timestamp"),
        find_lsh_sim("q1.lsh_hash", "q2.lsh_hash").alias("lsh_sim")
    ).sort("q1_timestamp", "q2_timestamp")

    # Duplicate candidates have a high enough LSH similarity count
    lsh_cand_df = lsh_sim_df.filter(lsh_sim_df.lsh_sim >= config.LSH_SIMILARITY_BAND_COUNT)

    # Compare MinHash jaccard similarity scores for duplicate candidates
    find_mh_js = udf(lambda x, y: mh.jaccard_sim_score(x, y))
    mh_cand_df = lsh_cand_df.withColumn("mh_js", find_mh_js(lsh_cand_df.q1_min_hash, lsh_cand_df.q2_min_hash))

    # Duplicate candidates need to have a high enough MinHash Jaccard similarity score
    dup_cand_df = mh_cand_df.filter(mh_cand_df.mh_js >= config.DUP_QUESTION_MIN_HASH_THRESHOLD)

    print("number of partitions: ", dup_cand_df.rdd.getNumPartitions())
    dup_cand_df.foreachPartition(lambda rdd: store_dup_cand_redis(rdd))
    """


# understanding LSH parameters:
# https://towardsdatascience.com/understanding-locality-sensitive-hashing-49f6d1f6134
# https://santhoshhari.github.io/Locality-Sensitive-Hashing/
# LSH at UBER: https://eng.uber.com/lsh/
def run_minhash_lsh():
    df = util.read_all_json_from_bucket(sql_context, config.S3_BUCKET_BATCH_PREPROCESSED)

    #  Create and save MinHash and LSH if not exist or load them from file
    if(not os.path.isfile(config.MIN_HASH_PICKLE) or not os.path.isfile(config.LSH_PICKLE)):
        mh = MinHash(config.MIN_HASH_K_VALUE)
        lsh = LSH(config.LSH_NUM_BANDS, config.LSH_BAND_WIDTH, config.LSH_NUM_BUCKETS)
        util.save_pickle_file(mh, config.MIN_HASH_PICKLE)
        util.save_pickle_file(lsh, config.LSH_PICKLE)
    else:
        mh = util.load_pickle_file(config.MIN_HASH_PICKLE)
        lsh = util.load_pickle_file(config.LSH_PICKLE)

    # Compute MinHash/LSH hashes for every question
    if (config.LOG_DEBUG): print("[BATCH]: Calculating MinHash hashes and LSH hashes...")
    compute_minhash_lsh(df, mh, lsh)

    # Compute pairwise LSH similarities for questions within tags
    if (config.LOG_DEBUG): print("[BATCH]: Fetching questions, comparing LSH and MinHash, uploading duplicate candidates back to Redis...")
    find_dup_cands_within_tags(mh, lsh)


def main():
    spark_conf = SparkConf().setAppName("Spark CustomMinHashLSH").set("spark.cores.max", "30")

    global sc
    sc = SparkContext(conf=spark_conf)
    sc.setLogLevel("ERROR")
        sc.addFile(os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/lib/MinHash.py")
            sc.addFile(os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/lib/LSH.py")
    sc.addFile(os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/lib/CustomMinHashLSH.py")
    sc.addFile(os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/lib/util.py")
    sc.addFile(os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/config/config.py")

    global sql_context
    sql_context = SQLContext(sc)

    start_time = time.time()
    run_minhash_lsh()
    end_time = time.time()
    print("Spark Custom MinHashLSH run time (seconds): {0} seconds".format(end_time - start_time))


if(__name__ == "__main__"):
    main()
