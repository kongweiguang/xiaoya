/**
 * Copyright(c) Live2D Inc. All rights reserved.
 *
 * Use of this source code is governed by the Live2D Open Software license
 * that can be found at https://www.live2d.com/eula/live2d-open-software-license-agreement_en.html.
 */
import { csmString } from '@framework/type/csmstring';
import * as $ from './cubismmotionsyncenginelib';
import { EngineType, MappingInfoListMapper } from './cubismmotionsyncutil';
export type CubismMotionSyncContext = Live2DCubismMotionSyncCore.Context;
export type CubismMotionSyncContextConfig = unknown;
export declare class CubismMotionSyncEngineLib {
    getEngineVersion(): number;
    getEngineName(): csmString;
    initializeEngine(engineConfig: number): boolean;
    /** 最后一批输入不会再被下一次分析替换，引擎销毁时必须显式释放。 */
    disposeEngine(): void;
    createContext(type: EngineType, contextConfig: CubismMotionSyncContextConfig, mappingInfoList: MappingInfoListMapper, mappingInfoListCount: number): CubismMotionSyncContext;
    clearContext(context: CubismMotionSyncContext): void;
    deleteContext(context: CubismMotionSyncContext): void;
    getRequireSampleCount(context: CubismMotionSyncContext): number;
    analyze(context: CubismMotionSyncContext, samples: Array<number>, samplesOffset: number, sampleCount: number, analysisResultPtr: number, analysisConfigPtr: number): boolean;
    isInitialized(): boolean;
    private _isEngineInitialized;
    private _analyzeSamplesPtr;
}
export declare namespace Live2DCubismMotionSyncFramework {
    type CubismMotionSyncContext = $.CubismMotionSyncContext;
    const CubismMotionSyncEngineLib: typeof $.CubismMotionSyncEngineLib;
    type CubismMotionSyncEngineLib = $.CubismMotionSyncEngineLib;
}
