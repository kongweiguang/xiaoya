/**
 * Copyright(c) Live2D Inc. All rights reserved.
 *
 * Use of this source code is governed by the Live2D Open Software license
 * that can be found at https://www.live2d.com/eula/live2d-open-software-license-agreement_en.html.
 */
import { csmVector } from '@framework/type/csmvector';
import { CubismMotionSyncEngineAnalysisResult } from './cubismmotionsyncengineanalysisresult';
import { CubismMotionSyncEngineMappingInfo } from './cubismmotionsyncenginemappinginfo';
import * as $ from './cubismmotionsyncprocessorcri';
import { MotionSyncContext } from './cubismmotionsyncutil';
import { ICubismMotionSyncEngine } from './icubismmotionsyncengine';
import { ICubismMotionSyncProcessor } from './icubismmotionsyncprocessor';
export declare class CubismMotionSyncProcessorCRI extends ICubismMotionSyncProcessor {
    getSampleRate(): number;
    getBitDepth(): number;
    /** 保留官方分析流程，临时配置在 finally 释放，且原生缓冲按字节分配。 */
    Analyze(samples: csmVector<number>, beginIndex: number, blendRatio: number, smoothing: number, audioLevelEffectRatio: number, analysisResult: CubismMotionSyncEngineAnalysisResult): CubismMotionSyncEngineAnalysisResult;
    constructor(engine: ICubismMotionSyncEngine, contextHandle: MotionSyncContext, mappingList: csmVector<CubismMotionSyncEngineMappingInfo>, sampleRate: number, bitDepth: number);
    /** 关闭处理器时一并释放分析结构，挂断不能给全局引擎遗留原生缓冲。 */
    Close(): void;
    /** 缓冲可被初始化失败路径回收，因此允许指针尚未创建或已经清零。 */
    release(): void;
    private _sampleRate;
    private _bitDepth;
    private _analysisConfigNativePtr;
    private _analysisResultNativePtr;
}
export declare namespace Live2DCubismMotionSyncFramework {
    const CubismMotionSyncProcessorCRI: typeof $.CubismMotionSyncProcessorCRI;
    type CubismMotionSyncProcessorCRI = $.CubismMotionSyncProcessorCRI;
}
